from __future__ import annotations

import html
import http.client
import json
import hmac
import hashlib
import http.server
import os
import queue
import re
import secrets
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from unified_music import (
    RecordingCandidate, UnifiedRecording, bounded_search, merge_candidates,
    normalize as normalize_recording_text, strict_match, unified_eligible, version_fingerprint,
)

from lx_fallback import (
    AppleTrack,
    LxCandidate,
    LxFallbackClient,
    LxFallbackError,
    LxFallbackStopped,
    LxSearchItem,
    MP3_320_QUALITY,
    best_download_quality,
    fallback_is_eligible,
    is_lossless_quality,
    map_server_music_path,
    parse_duration_seconds,
    resolve_queue_candidate,
)

from apple_music_library import (
    ExistingTrack,
    LIBRARY_ROOT,
    find_existing_track,
    refresh_index as refresh_apple_music_library_index,
    read_media_identity as read_apple_media_identity,
    song_id_from_url as apple_music_song_id_from_url,
)
from apple_music_results import (
    effective_download_return_code,
    parse_verified_existing_media,
)
from word_lyrics import sync_word_lyrics_for_paths as _sync_word_lyrics_for_paths, enqueue_completion_job
from word_lyrics_automation.lyric_messages import LyricMessageStore
from download_history import remove_deleted_media_history
from word_lyrics_automation.notifications import (
    NotificationStore,
    WordLyricNotification,
)
from word_lyrics_automation.queue import QueueStore
from download_progress import (
    format_iec_size,
    parse_gamdl_track,
    parse_gamdl_transfer,
    progress_bar,
)
from task_cards import (
    ACTIVE_STATUSES as TASK_CARD_ACTIVE_STATUSES,
    SOURCE_LABELS,
    TaskCardStore,
    delete_recorded_paths,
    format_task_card,
    json_paths,
    task_keyboard,
)
from bilibili_ingest import (
    AUDIO_ROOT as BILIBILI_AUDIO_ROOT,
    DB_PATH as BILIBILI_DB_PATH,
    ENABLED as BILIBILI_ENABLED,
    PLAYLIST_MAX_ITEMS as BILIBILI_PLAYLIST_MAX_ITEMS,
    STATE_DIR as BILIBILI_STATE_DIR,
    VIDEO_ROOT as BILIBILI_VIDEO_ROOT,
    BilibiliIngestError,
    cookie_status as bilibili_cookie_status,
    download as download_bilibili,
    extract_urls as extract_bilibili_urls,
    probe_url as probe_bilibili_url,
)
from youtube_ingest import (
    AUDIO_ROOT as YOUTUBE_AUDIO_ROOT,
    DB_PATH as YOUTUBE_DB_PATH,
    ENABLED as YOUTUBE_ENABLED,
    PLAYLIST_MAX_ITEMS as YOUTUBE_PLAYLIST_MAX_ITEMS,
    STATE_DIR as YOUTUBE_STATE_DIR,
    VIDEO_ROOT as YOUTUBE_VIDEO_ROOT,
    YouTubeIngestError,
    cookie_status as youtube_cookie_status,
    download as download_youtube,
    extract_urls as extract_youtube_urls,
    is_collection_hint as is_youtube_collection_hint,
    is_music_url as is_youtube_music_url,
    probe_url as probe_youtube_url,
    trigger_emby_scan,
    trigger_navidrome_scan,
    validate_url as validate_youtube_url,
)


APPLE_MUSIC_URL_RE = re.compile(
    r"https?://(?:beta\.music\.apple\.com|geo\.music\.apple\.com|music\.apple\.com)/[^\s<>'\"]+",
    re.IGNORECASE,
)
STORE_FRONT_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,;:!?，。；：！？)]）】>\"'"
VALID_MODES = {"alac", "atmos", "both"}
MEDIA_OUTPUT_SUFFIXES = {".m4a", ".mp4", ".m4v", ".ec3", ".flac", ".mp3"}
VALID_LYRIC_SIDECAR_FORMATS = {"lrc", "srt", "ttml"}
GAMDL_FINISHED_WITH_ERRORS_RE = re.compile(r"Finished with ([1-9]\d*) error", re.IGNORECASE)
WRAPPER_M3U8_FAILURE_RE = re.compile(
    r"Wrapper m3u8 request .* failed after \d+ attempt",
    re.IGNORECASE,
)
TRANSIENT_GAMDL_FAILURE_RE = re.compile(
    r"("
    r"Wrapper m3u8 request .* failed after \d+ attempt|"
    r"Error fetching account info|"
    r"Error fetching index\.js page|"
    r"\b(?:Connect|Read|Write|Pool)(?:Error|Timeout)\b|"
    r"All connection attempts failed|"
    r"Temporary failure in name resolution|"
    r"Network is unreachable|"
    r"UNEXPECTED_EOF_WHILE_READING|"
    r"SSLEOFError|"
    r"TLSV1_ALERT_INTERNAL_ERROR|"
    r"IncompleteReadError|"
    r"Remote end closed connection|"
    r"Connection reset by peer|"
    r"Read timed out|"
    r"Max retries exceeded"
    r")",
    re.IGNORECASE,
)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_lyric_sidecar_formats(value: str) -> list[str]:
    formats: list[str] = []
    for item in value.split(","):
        lyric_format = item.strip().lower()
        if not lyric_format:
            continue
        if lyric_format not in VALID_LYRIC_SIDECAR_FORMATS:
            print(
                f"ignored unsupported lyric sidecar format: {lyric_format}",
                flush=True,
            )
            continue
        if lyric_format not in formats:
            formats.append(lyric_format)
    return formats


STATE_DIR = Path(env("AMDL_STATE_DIR", "/state"))
OFFSET_PATH = STATE_DIR / "telegram_offset.json"
JOURNAL_PATH = STATE_DIR / "jobs.ndjson"
LOG_DIR = Path(env("AMDL_LOG_DIR", "/state/logs"))
TEMP_PATH = Path(env("AMDL_TEMP_PATH", "/state/tmp"))

TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_BASE_URL = env(
    "TELEGRAM_API_BASE_URL", "https://api.telegram.org"
).rstrip("/")
TELEGRAM_PROXY_URL = env("TELEGRAM_PROXY_URL")
TELEGRAM_POLL_PROXY_URL = env("TELEGRAM_POLL_PROXY_URL", TELEGRAM_PROXY_URL)
TELEGRAM_API_ATTEMPTS = max(1, int(env("TELEGRAM_API_ATTEMPTS", "2") or "2"))
TELEGRAM_POLL_MAX_CONSECUTIVE_ERRORS = max(
    0,
    int(env("TELEGRAM_POLL_MAX_CONSECUTIVE_ERRORS", "30") or "30"),
)
TELEGRAM_POLL_TIMEOUT_SECONDS = min(
    50,
    max(0, int(env("TELEGRAM_POLL_TIMEOUT_SECONDS", "0") or "0")),
)
TELEGRAM_POLL_REQUEST_TIMEOUT_SECONDS = max(
    TELEGRAM_POLL_TIMEOUT_SECONDS + 5,
    int(
        env(
            "TELEGRAM_POLL_REQUEST_TIMEOUT_SECONDS",
            str(TELEGRAM_POLL_TIMEOUT_SECONDS + 10),
        )
        or str(TELEGRAM_POLL_TIMEOUT_SECONDS + 10)
    ),
)
TELEGRAM_POLL_IDLE_SECONDS = max(
    0.2,
    float(env("TELEGRAM_POLL_IDLE_SECONDS", "0.5") or "0.5"),
)
TELEGRAM_POLL_ERROR_DELAY_SECONDS = max(
    0.5,
    float(env("TELEGRAM_POLL_ERROR_DELAY_SECONDS", "1") or "1"),
)
TELEGRAM_SEND_ATTEMPTS = max(1, int(env("TELEGRAM_SEND_ATTEMPTS", "5") or "5"))
TELEGRAM_SEND_RETRY_DELAY_SECONDS = float(
    env("TELEGRAM_SEND_RETRY_DELAY_SECONDS", "3") or "3"
)
WORD_LYRICS_NOTIFICATION_POLL_SECONDS = max(
    2.0,
    float(env("WORD_LYRICS_NOTIFICATION_POLL_SECONDS", "5") or "5"),
)
TASK_CARD_UPDATE_SECONDS = max(
    2.0,
    float(env("AMDL_TASK_CARD_UPDATE_SECONDS", "5") or "5"),
)
ALLOWED_CHAT_IDS = {
    item.strip()
    for item in env("TELEGRAM_ALLOWED_CHAT_IDS").split(",")
    if item.strip()
}
SYN_CHAT_ENABLED = env("SYNOLOGY_CHAT_ENABLED", "false").lower() == "true"
HEALTH_SERVER_ENABLED = env_flag("AMDL_HEALTH_SERVER_ENABLED", True)
SYN_CHAT_BIND = env("SYNOLOGY_CHAT_BIND", "0.0.0.0")
SYN_CHAT_PORT = int(env("SYNOLOGY_CHAT_PORT", "30871"))
SYN_CHAT_WEBHOOK_PATH = env("SYNOLOGY_CHAT_WEBHOOK_PATH", "/synology-chat")
SYN_CHAT_OUTGOING_TOKEN = env("SYNOLOGY_CHAT_OUTGOING_TOKEN")
SYN_CHAT_INCOMING_WEBHOOK_URL = env("SYNOLOGY_CHAT_INCOMING_WEBHOOK_URL")
SYN_CHAT_REPLY_PROXY_URL = env("SYNOLOGY_CHAT_REPLY_PROXY_URL")
SYN_CHAT_ALLOWED_USER_IDS = {
    item.strip()
    for item in env("SYNOLOGY_CHAT_ALLOWED_USER_IDS").split(",")
    if item.strip()
}
DEFAULT_MODE = env("AMDL_DEFAULT_MODE", "alac").lower()
LANGUAGE = env("AMDL_LANGUAGE", "zh-CN")
ALLOW_AAC_FALLBACK = env("AMDL_ALLOW_AAC_FALLBACK", "false").lower() == "true"
BACKEND = env("AMDL_BACKEND", "gamdl").lower()
WRAPPER_MANAGER_ENDPOINT = env("AMDL_WRAPPER_MANAGER_ENDPOINT", "wrapper-manager:8080")
WRAPPER_MANAGER_SECURE = env("AMDL_WRAPPER_MANAGER_SECURE", "false").lower() == "true"
DOWNLOAD_PROXY_URL = env("AMDL_DOWNLOAD_PROXY_URL", "")
APPLEMUSICDECRYPT_ROOT = Path(env("APPLEMUSICDECRYPT_ROOT", "/app/vendor/AppleMusicDecrypt"))
APPLEMUSICDECRYPT_RUNNER = Path(env("APPLEMUSICDECRYPT_RUNNER", "/app/amdecrypt_once.py"))
WRAPPER_URL = env("WRAPPER_URL", "http://wrapper")
WRAPPER_DECRYPT_HOST = env("WRAPPER_DECRYPT_HOST", "wrapper")
WRAPPER_DECRYPT_PORT = int(env("WRAPPER_DECRYPT_PORT", "10020") or "10020")
# Keep the legacy aliases local to the orchestration code.  The official
# gamdl 3.8.x CLI is invoked with wrapper-v2's HTTP URL and WV2D TCP endpoint.
WRAPPER_ACCOUNT_URL = WRAPPER_URL
WRAPPER_DECRYPT_IP = f"{WRAPPER_DECRYPT_HOST}:{WRAPPER_DECRYPT_PORT}"
WRAPPER_M3U8_IP = WRAPPER_DECRYPT_IP
WRAPPER_FORWARD_ENABLED = env("WRAPPER_FORWARD_ENABLED", "false").lower() == "true"
WRAPPER_FORWARD_HOST = env("WRAPPER_FORWARD_HOST", "wrapper-vm")
WRAPPER_FORWARD_ACCOUNT_TARGET = env("WRAPPER_FORWARD_ACCOUNT_TARGET", f"{WRAPPER_FORWARD_HOST}:30020")
WRAPPER_FORWARD_DECRYPT_TARGET = env("WRAPPER_FORWARD_DECRYPT_TARGET", f"{WRAPPER_FORWARD_HOST}:10020")
WRAPPER_FORWARD_M3U8_TARGET = env("WRAPPER_FORWARD_M3U8_TARGET", f"{WRAPPER_FORWARD_HOST}:20020")
ALAC_OUTPUT_PATH = Path(env("AMDL_ALAC_OUTPUT_PATH", "/music/ALAC"))
ATMOS_OUTPUT_PATH = Path(env("AMDL_ATMOS_OUTPUT_PATH", "/music/Atmos"))
GAMDL_RUNNER = Path(env("AMDL_GAMDL_RUNNER", "/app/gamdl_with_storefront.py"))
GAMDL_USE_URL_STOREFRONT = env("AMDL_GAMDL_USE_URL_STOREFRONT", "true").lower() == "true"
GAMDL_NO_OUTPUT_TIMEOUT_SECONDS = int(env("AMDL_GAMDL_NO_OUTPUT_TIMEOUT_SECONDS", "900"))
GAMDL_LYRIC_SIDECAR_FORMATS = parse_lyric_sidecar_formats(
    env("AMDL_GAMDL_LYRIC_SIDECAR_FORMATS", "lrc,ttml")
)
GAMDL_LYRIC_SIDECAR_TIMEOUT_SECONDS = int(
    env("AMDL_GAMDL_LYRIC_SIDECAR_TIMEOUT_SECONDS", "300") or "300"
)
GAMDL_LYRIC_SIDECAR_OVERWRITE = env_flag("AMDL_GAMDL_LYRIC_SIDECAR_OVERWRITE", False)
WRAPPER_M3U8_FAILURE_JOB_RETRIES = max(
    0, int(env("AMDL_WRAPPER_M3U8_FAILURE_JOB_RETRIES", "1") or "1")
)
WRAPPER_M3U8_FAILURE_JOB_RETRY_DELAY_SECONDS = float(
    env("AMDL_WRAPPER_M3U8_FAILURE_JOB_RETRY_DELAY_SECONDS", "300") or "300"
)
TRANSIENT_FAILURE_JOB_RETRIES = max(
    0,
    int(
        env(
            "AMDL_TRANSIENT_FAILURE_JOB_RETRIES",
            env("AMDL_WRAPPER_M3U8_FAILURE_JOB_RETRIES", "1"),
        )
        or "1"
    ),
)
TRANSIENT_FAILURE_JOB_RETRY_DELAY_SECONDS = float(
    env("AMDL_TRANSIENT_FAILURE_JOB_RETRY_DELAY_SECONDS", "45") or "45"
)
GAMDL_LYRIC_SIDECAR_RETRIES = max(
    0, int(env("AMDL_GAMDL_LYRIC_SIDECAR_RETRIES", "1") or "1")
)
GAMDL_LYRIC_SIDECAR_RETRY_DELAY_SECONDS = float(
    env("AMDL_GAMDL_LYRIC_SIDECAR_RETRY_DELAY_SECONDS", "15") or "15"
)
WRAPPER_RECOVERY_ENABLED = env("AMDL_WRAPPER_RECOVERY_ENABLED", "true").lower() == "true"
WRAPPER_RECOVERY_DOCKER_SOCKET = env(
    "AMDL_WRAPPER_RECOVERY_DOCKER_SOCKET",
    "/var/run/docker.sock",
)
WRAPPER_RECOVERY_DOCKER_CONTAINER = env(
    "AMDL_WRAPPER_RECOVERY_DOCKER_CONTAINER",
    "apple-music-wrapper-vm",
)
WRAPPER_RECOVERY_TIMEOUT_SECONDS = max(
    10, int(env("AMDL_WRAPPER_RECOVERY_TIMEOUT_SECONDS", "25") or "25")
)
WRAPPER_RECOVERY_WAIT_SECONDS = max(
    30, int(env("AMDL_WRAPPER_RECOVERY_WAIT_SECONDS", "180") or "180")
)
WRAPPER_RECOVERY_MIN_INTERVAL_SECONDS = max(
    0.0, float(env("AMDL_WRAPPER_RECOVERY_MIN_INTERVAL_SECONDS", "180") or "180")
)
SEARCH_ENABLED = env("AMDL_SEARCH_ENABLED", "true").lower() == "true"
SEARCH_COUNTRY = env("AMDL_SEARCH_COUNTRY", "CN").upper() or "CN"
SEARCH_LANGUAGE = env("AMDL_SEARCH_LANGUAGE", "zh_cn") or "zh_cn"
SEARCH_LIMIT = min(50, max(1, int(env("AMDL_SEARCH_LIMIT", "32") or "32")))
SEARCH_PAGE_SIZE = min(8, max(1, int(env("AMDL_SEARCH_PAGE_SIZE", "8") or "8")))
SEARCH_PROXY_URL = env("AMDL_SEARCH_PROXY_URL", TELEGRAM_PROXY_URL)
SEARCH_ATTEMPTS = max(1, int(env("AMDL_SEARCH_ATTEMPTS", "4") or "4"))
SEARCH_TIMEOUT_SECONDS = max(5, int(env("AMDL_SEARCH_TIMEOUT_SECONDS", "25") or "25"))
SEARCH_RETRY_DELAY_SECONDS = float(env("AMDL_SEARCH_RETRY_DELAY_SECONDS", "2") or "2")
SEARCH_RESULT_TTL_SECONDS = max(
    60, int(env("AMDL_SEARCH_RESULT_TTL_SECONDS", "1800") or "1800")
)
SEARCH_CALLBACK_PREFIX = "amdl"
WORD_LYRICS_MAC_CALLBACK_PREFIX = "wlmac"
UNIFIED_MUSIC_ENABLED = env_flag("AMDL_UNIFIED_MUSIC_ENABLED", False)
UNIFIED_SEARCH_TIMEOUT_SECONDS = max(2.0, float(env("AMDL_UNIFIED_SEARCH_TIMEOUT_SECONDS", "25")))
SQ_MUSIC_ENABLED = env_flag("AMDL_SQ_MUSIC_ENABLED", False)
SQ_MUSIC_BASE_URL = env("AMDL_SQ_MUSIC_BASE_URL", "http://sqmusic-server:8099")
SQ_MUSIC_AUTH_FILE = env("AMDL_SQ_AUTH_FILE", "/run/secrets/sq-auth.json")
SQ_MUSIC_STAGING_PATH = Path(env("AMDL_SQ_MUSIC_STAGING_PATH", "/sq-staging"))
SQ_MUSIC_OUTPUT_PATH = Path(env("AMDL_SQ_MUSIC_OUTPUT_PATH", "/music/SQ Music"))
SQ_MUSIC_SERVER_ROOT = env("AMDL_SQ_MUSIC_SERVER_ROOT", "/music")
SQ_MUSIC_SOURCES = tuple(item.strip() for item in env("AMDL_SQ_MUSIC_SOURCES", "kw,netease,qqvip").split(",") if item.strip())

LX_FALLBACK_ENABLED = env_flag("AMDL_LX_FALLBACK_ENABLED", False)
LX_FALLBACK_BASE_URL = env(
    "AMDL_LX_FALLBACK_BASE_URL",
    "http://lxserver-test:9527",
).rstrip("/")
LX_FALLBACK_AUTH_FILE = env("AMDL_LX_FALLBACK_AUTH_FILE", "")
LX_FALLBACK_OUTPUT_PATH = Path(
    env("AMDL_LX_FALLBACK_OUTPUT_PATH", "/music/LX Music")
)
LX_FALLBACK_SERVER_MUSIC_ROOT = env(
    "AMDL_LX_FALLBACK_SERVER_MUSIC_ROOT",
    "/server/music",
)
LX_FALLBACK_QUALITY = "flac"
LX_FALLBACK_ALLOW_320 = env_flag("AMDL_LX_FALLBACK_ALLOW_320", True)
LX_FALLBACK_ALLOW_CATALOG_API_3076 = env_flag(
    "AMDL_LX_FALLBACK_ALLOW_CATALOG_API_3076",
    True,
)
LX_FALLBACK_METADATA_COUNTRIES = tuple(
    dict.fromkeys(
        country
        for raw_country in env(
            "AMDL_LX_FALLBACK_METADATA_COUNTRIES",
            f"{SEARCH_COUNTRY},HK,TW,SG,US",
        ).split(",")
        if (country := raw_country.strip().upper())
        and re.fullmatch(r"[A-Z]{2}", country)
    )
)
LX_FALLBACK_SEARCH_SOURCES = tuple(
    dict.fromkeys(
        source.strip().lower()
        for source in env(
            "AMDL_LX_FALLBACK_SEARCH_SOURCES",
            "kw,tx,wy,mg,kg",
        ).split(",")
        if source.strip()
    )
)
LX_FALLBACK_REQUEST_TIMEOUT_SECONDS = max(
    5.0,
    float(env("AMDL_LX_FALLBACK_REQUEST_TIMEOUT_SECONDS", "30") or "30"),
)
LX_FALLBACK_QUEUE_TIMEOUT_SECONDS = max(
    60.0,
    float(env("AMDL_LX_FALLBACK_QUEUE_TIMEOUT_SECONDS", "1800") or "1800"),
)
LX_FALLBACK_POLL_SECONDS = max(
    0.5,
    float(env("AMDL_LX_FALLBACK_POLL_SECONDS", "2") or "2"),
)
LX_FALLBACK_MAX_CANDIDATES = min(
    40,
    max(1, int(env("AMDL_LX_FALLBACK_MAX_CANDIDATES", "20") or "20")),
)
LX_SEARCH_PAGE_SIZE = min(
    8,
    max(1, int(env("AMDL_LX_SEARCH_PAGE_SIZE", "6") or "6")),
)
LX_SEARCH_MAX_RESULTS = min(
    100,
    max(1, int(env("AMDL_LX_SEARCH_MAX_RESULTS", "80") or "80")),
)
YOUTUBE_CALLBACK_PREFIX = "yt"
BILIBILI_CALLBACK_PREFIX = "bili"
VIDEO_DELETE_CALLBACK_PREFIX = "vdel"
LOCAL_MEDIA_TASK_PATH = "/api/v1/media-tasks"
VIDEO_DELETE_PAGE_SIZE = min(
    8, max(1, int(env("VIDEO_DELETE_PAGE_SIZE", "6") or "6"))
)
VIDEO_DELETE_TTL_SECONDS = max(
    120, int(env("VIDEO_DELETE_TTL_SECONDS", "600") or "600")
)
YOUTUBE_CHOICE_TTL_SECONDS = max(
    60, int(env("YT_CHOICE_TTL_SECONDS", "1800") or "1800")
)
YOUTUBE_CHOICE_METADATA_ENABLED = env_flag("YT_CHOICE_METADATA_ENABLED", True)
YOUTUBE_CHOICE_METADATA_TIMEOUT_SECONDS = max(
    0.5,
    float(env("YT_CHOICE_METADATA_TIMEOUT_SECONDS", "2") or "2"),
)
YOUTUBE_AUDIO_DISPLAY_ROOT = env(
    "YT_AUDIO_DISPLAY_ROOT",
    "/youtube-music",
)
YOUTUBE_VIDEO_DISPLAY_ROOT = env(
    "YT_VIDEO_DISPLAY_ROOT",
    "/youtube-video",
)
BILIBILI_CHOICE_TTL_SECONDS = max(
    60, int(env("BILI_CHOICE_TTL_SECONDS", "1800") or "1800")
)
BILIBILI_AUDIO_DISPLAY_ROOT = env(
    "BILI_AUDIO_DISPLAY_ROOT",
    "/bilibili-music",
)
BILIBILI_VIDEO_DISPLAY_ROOT = env(
    "BILI_VIDEO_DISPLAY_ROOT",
    "/bilibili-video",
)

stop_event = threading.Event()
word_lyrics_notification_heartbeat = time.monotonic()
WORD_LYRICS_BOT_STARTED_AT = time.time()
job_queue: queue.Queue["Job"] = queue.Queue()
current_job_lock = threading.Lock()
current_job: "Job | None" = None
download_progress_lock = threading.Lock()
current_download_progress: "DownloadProgress | None" = None
task_card_store = TaskCardStore(STATE_DIR / "task-cards.sqlite3")
task_card_update_lock = threading.Lock()
task_card_last_update: dict[int, float] = {}
task_process_lock = threading.Lock()
active_task_process: "subprocess.Popen[str] | None" = None
active_task_process_id = 0
wrapper_recovery_lock = threading.Lock()
last_wrapper_recovery_at = 0.0


@dataclass(frozen=True)
class Job:
    source: str
    chat_id: str
    message_id: str
    urls: list[str]
    mode: str
    kind: str
    received_at: float
    task_id: int = 0
    payload: dict[str, Any] | None = None

    @property
    def label(self) -> str:
        return f"{self.source}:{self.chat_id}/{self.message_id}"


@dataclass(frozen=True)
class DownloadProgress:
    job_label: str
    mode: str
    phase: str
    started_at: float
    updated_at: float
    url_index: int
    url_total: int
    track_index: int = 0
    track_total: int = 0
    title: str = ""
    percent: float | None = None
    total_bytes: int | None = None
    total_estimated: bool = False
    speed_bytes_per_second: float | None = None
    eta: str = ""
    fragment: int | None = None
    fragments: int | None = None


@dataclass(frozen=True)
class SearchResult:
    title: str
    artist: str
    album: str
    url: str
    track_id: str
    release_year: str = ""
    genre: str = ""
    artwork_url: str = ""
    duration_seconds: float | None = None
    isrc: str = ""
    unified_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class LxFallbackOutcome:
    success: bool
    signature: str
    message: str
    title: str = ""
    artist: str = ""
    album: str = ""
    queue_id: str = ""
    paths: tuple[Path, ...] = ()
    created_paths: tuple[Path, ...] = ()
    scan_triggered: bool = False
    quality: str = ""
    sample_rate: int = 0
    bit_depth: int = 0


@dataclass(frozen=True)
class SearchSession:
    chat_id: str
    message_id: str
    scope_key: str
    query: str
    mode: str
    results: list[SearchResult]


@dataclass(frozen=True)
class LxSearchSession:
    query: str
    apple_page: int
    results: list[LxSearchItem]


@dataclass(frozen=True)
class YouTubeChoice:
    chat_id: str
    actor_id: str
    url: str
    title: str
    uploader: str
    item_count: int
    is_collection: bool
    truncated: bool


@dataclass(frozen=True)
class BilibiliChoice:
    chat_id: str
    actor_id: str
    url: str
    title: str
    uploader: str
    item_count: int
    is_collection: bool
    truncated: bool


@dataclass(frozen=True)
class VideoDeleteEntry:
    platform: str
    media_id: str
    source_url: str
    title: str
    output_path: str
    downloaded_at: float


@dataclass(frozen=True)
class VideoDeleteSession:
    chat_id: str
    actor_id: str
    entries: tuple[VideoDeleteEntry, ...]


class VideoDeleteError(RuntimeError):
    pass


search_cache_lock = threading.Lock()
search_cache: dict[str, tuple[float, str, SearchResult]] = {}
search_session_cache: dict[str, tuple[float, SearchSession]] = {}
lx_search_session_cache: dict[str, tuple[float, LxSearchSession]] = {}
active_search_session_by_chat: dict[str, str] = {}
youtube_choice_cache: dict[str, tuple[float, YouTubeChoice]] = {}
youtube_choice_lock = threading.Lock()
bilibili_choice_cache: dict[str, tuple[float, BilibiliChoice]] = {}
bilibili_choice_lock = threading.Lock()
video_delete_session_cache: dict[str, tuple[float, VideoDeleteSession]] = {}
video_delete_cache_lock = threading.Lock()
online_video_library_lock = threading.Lock()
log_lock = threading.Lock()
journal_lock = threading.Lock()


def redact_sensitive_text(message: object) -> str:
    text = str(message)
    if TOKEN:
        text = text.replace(TOKEN, "[REDACTED_TELEGRAM_TOKEN]")
    text = re.sub(
        r"bot[0-9]{6,12}:[A-Za-z0-9_-]{20,}",
        "bot[REDACTED_TELEGRAM_TOKEN]",
        text,
    )
    text = re.sub(
        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
        "[REDACTED_JWT]",
        text,
    )
    return text


def log(message: object) -> None:
    with log_lock:
        print(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"{redact_sensitive_text(message)}",
            flush=True,
        )


def build_opener(proxy_url: str = "") -> urllib.request.OpenerDirector:
    if proxy_url:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


TELEGRAM_OPENER = build_opener(TELEGRAM_PROXY_URL)
TELEGRAM_POLL_OPENER = build_opener(TELEGRAM_POLL_PROXY_URL)
SYN_CHAT_REPLY_OPENER = build_opener(SYN_CHAT_REPLY_PROXY_URL)
SEARCH_OPENER = build_opener(SEARCH_PROXY_URL)
DIRECT_OPENER = build_opener("")


def retryable_network_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return 500 <= exc.code < 600 or exc.code == 429
    return isinstance(
        exc,
        (
            urllib.error.URLError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
            TimeoutError,
            ConnectionError,
            OSError,
        ),
    )


def read_response_text(response: Any) -> str:
    return response.read().decode("utf-8")


def telegram_http_error_description(exc: urllib.error.HTTPError) -> str:
    try:
        body = read_response_text(exc)
    except Exception:
        return str(exc)
    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        return body[:500] or str(exc)
    description = result.get("description") if isinstance(result, dict) else None
    return str(description or body[:500] or exc)


def telegram_api(method: str, payload: dict[str, Any] | None = None, timeout: int = 70) -> Any:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")
    url = f"{TELEGRAM_API_BASE_URL}/bot{TOKEN}/{method}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    last_error: Exception | None = None
    # getUpdates is a continuous receive loop. Retrying it here would stack
    # per-attempt backoffs and make new messages appear late when a proxy drops
    # one request. The outer poll loop performs its own fast retry instead.
    max_attempts = 1 if method == "getUpdates" else TELEGRAM_API_ATTEMPTS
    opener = TELEGRAM_POLL_OPENER if method == "getUpdates" else TELEGRAM_OPENER
    for attempt in range(1, max_attempts + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                body = read_response_text(response)
            result = json.loads(body)
            if not result.get("ok"):
                raise RuntimeError(f"Telegram API error for {method}: {result}")
            return result.get("result")
        except Exception as exc:
            last_error = exc
            if attempt >= max_attempts or not retryable_network_error(exc):
                break
            if method != "getUpdates" or attempt > 1:
                log(
                    f"telegram api {method} network error on attempt "
                    f"{attempt}/{max_attempts}: {exc}; retrying"
                )
            time.sleep(min(10.0, 1.5 * attempt))
    assert last_error is not None
    if isinstance(last_error, urllib.error.HTTPError):
        description = telegram_http_error_description(last_error)
        raise RuntimeError(
            f"Telegram API {method} HTTP {last_error.code}: {description}"
        ) from last_error
    raise last_error


def send_message(
    chat_id: str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    parse_mode_name: str | None = None,
) -> dict[str, Any] | None:
    payload = {
        "chat_id": chat_id,
        "text": text[:3900],
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode_name:
        payload["parse_mode"] = parse_mode_name
    last_error: Exception | None = None
    for attempt in range(1, TELEGRAM_SEND_ATTEMPTS + 1):
        try:
            result = telegram_api("sendMessage", payload, timeout=30)
            return result if isinstance(result, dict) else None
        except Exception as exc:
            last_error = exc
            if attempt >= TELEGRAM_SEND_ATTEMPTS:
                break
            log(
                "failed to send Telegram message on attempt "
                f"{attempt}/{TELEGRAM_SEND_ATTEMPTS}: {exc}; retrying"
            )
            time.sleep(TELEGRAM_SEND_RETRY_DELAY_SECONDS * attempt)
    log(
        "failed to send Telegram message after "
        f"{TELEGRAM_SEND_ATTEMPTS} attempt(s): {last_error}"
    )
    return None



def sync_word_lyrics_for_paths(paths, *, source_url="", logger=None, notification_context=None):
    paths = list(paths)
    try:
        return _sync_word_lyrics_for_paths(paths, source_url=source_url, logger=logger,
            notification_context=notification_context)
    except Exception as exc:
        # A database outage must not silently discard every lyric outcome.
        context = notification_context or {}
        if context.get("source") == "telegram" and context.get("chat_id"):
            for path in paths:
                send_message(str(context["chat_id"]), "逐词歌词处理失败 ❌\n标题："
                    + Path(path).stem[:160] + "\n原因：后台任务登记入口不可用（"
                    + type(exc).__name__ + "）；现有音频和歌词已保留。")
        if logger:
            logger("[apple-word-lyrics] entry unavailable " + type(exc).__name__)
        return {"error": len(paths)}

def word_lyrics_provider_label(provider: str) -> str:
    return {
        "apple": "Apple Music 原生逐词歌词",
        "amll": "AMLL 在线逐词源",
        "netease_yrc": "网易云逐词源",
        "nas_whispercpp": "NAS 本地声学对齐",
        "kugou_reference_fusion": "酷狗逐词时间轴 + Apple 原版歌词校验",
        "mac_mlx_whisper": "Mac 声学对齐",
    }.get(provider, provider or "已通过严格校验的逐词源")


def format_word_lyrics_notification(
    notification: WordLyricNotification,
) -> str:
    metadata = notification.metadata
    result = notification.result
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    title = truncate_display_text(str(metadata.get("title") or "未知标题"), 120)
    artist = truncate_display_text(str(metadata.get("artist") or "未知艺人"), 120)
    if notification.terminal_status == "waiting_source":
        from datetime import datetime, timedelta, timezone
        observations = notification.payload.get("source_observations") or []
        history = notification.payload.get("source_diagnostics_history") or []
        if "source_discovery_budget_exhausted" in notification.last_error:
            reason = "本轮来源核验超时，诊断已保留，稍后自动重查。"
        elif any(value.get("instrumental_flag") for value in observations):
            reason = "已发现无词录音标记，还缺少独立的版本核验。"
        elif history and history[-1].get("reason") == "online_source_temporarily_unavailable":
            reason = "歌词来源暂时无法访问，检索尚未完成。"
        else:
            reason = "没有找到可信且版本匹配的歌词正文或逐词时间轴。"
        next_check = datetime.fromtimestamp(notification.next_attempt_at,
            timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        return "\n".join([
            "逐词歌词正在等待来源 ⏳", f"标题：{title}", f"艺人：{artist}",
            f"原因：{reason}", "当前阶段：尚未开始声学对齐。",
            f"下次自动重查：{next_check}（北京时间）。",
            "任务与诊断已保留，后续每六小时重查；同一等待状态只提醒一次，有最终结果会再通知。",
            f"后台任务：#{notification.job_id}",
        ])
    if notification.terminal_status == "awaiting_mac_consent":
        reason = truncate_display_text(
            redact_sensitive_text(notification.last_error or "NAS 本地对齐未通过质量门槛"),
            280,
        )
        return "\n".join(
            [
                "NAS 未能生成合格的逐词歌词，需要你的决定",
                f"标题：{title}",
                f"艺人：{artist}",
                f"NAS 结果：{reason}",
                "是否授权这一个任务调用 Mac 做高精度声学对齐？",
                "未经确认，音频不会传到 Mac。",
                "授权后，Mac 后台助手会在 Mac 在线时自动领取；无需打开 Codex。",
                f"后台任务：#{notification.job_id}",
            ]
        )
    if notification.terminal_status == "completed":
        if result.get("lyric_status") == "not_required":
            return "\n".join([
                "无词配乐已核验 ✅", f"标题：{title}", f"艺人：{artist}",
                "录音版本已由两处独立来源核对：这首没有歌词正文，无需生成逐词歌词。",
                "播放器已添加无词配乐说明，音频文件未被修改。",
                "Navidrome：已请求刷新。", f"后台任务：#{notification.job_id}",
            ])
        provider = str(result.get("provider") or metrics.get("provider") or "")
        provider_label = word_lyrics_provider_label(provider)
        reference_provider = str(metrics.get("mac_reference_provider") or "")
        if provider == "mac_mlx_whisper" and reference_provider == "kugou_official_krc":
            provider_label = "酷狗官方推荐歌词 + Mac 声学对齐"
        elif provider == "mac_mlx_whisper" and reference_provider:
            provider_label = "可信歌词正文 + Mac 声学对齐"
        cue_count = int(
            metrics.get("timed_words")
            or result.get("timed_words")
            or 0
        )
        lines = [
            ("主歌词逐词已就绪 ✅" if any(metrics.get(key) or metrics.get('mac_' + key)
                for key in ('line_timed_nonlexical_lines', 'line_timed_background_lines'))
                else "逐词歌词已就绪 ✅"),
            f"标题：{title}",
            f"艺人：{artist}",
            f"方式：{provider_label}",
        ]
        if cue_count:
            lines.append(f"逐词时间点：{cue_count}")
        humming = int(metrics.get('line_timed_nonlexical_lines') or metrics.get('mac_line_timed_nonlexical_lines') or 0)
        background = int(metrics.get('line_timed_background_lines') or metrics.get('mac_line_timed_background_lines') or 0)
        if metrics.get("character_changed_groups"):
            lines.append("汉字已逐字细分；新增的单字边界在原词组时间内估算。")
        if humming or background:
            lines.append(f"哼唱 {humming} 行、伴唱 {background} 处保留原文及逐行时间，没有编造逐词时间点。")
        lines.append("Navidrome：已请求刷新，可重新打开歌曲查看。")
        lines.append(f"后台任务：#{notification.job_id}")
        return "\n".join(lines)

    raw_error = notification.last_error or "达到安全重试上限"
    if "no_trusted_lyric_reference" in raw_error:
        error = "没有找到可信且版本匹配的歌词正文；已拒绝自由语音转写，未写入逐词歌词。"
    elif "free_transcription_without_reference_not_publishable" in raw_error:
        error = "候选结果只有自由语音识别、没有可信歌词正文，已按质量门槛拒绝发布。"
    elif "mac_fallback_declined_by_user" in raw_error:
        error = "已按你的选择不调用 Mac，保留原有歌词。"
    elif "acoustic/reference mismatch" in raw_error:
        error = "在线逐词来源和 NAS 声学对齐均未通过录音匹配校验，无法可靠地生成正文逐词时间。诊断和计算进度已保留。"
    else:
        error = truncate_display_text(
            redact_sensitive_text(raw_error),
            320,
        )
    return "\n".join(
        [
            "逐词歌词处理失败 ❌",
            f"标题：{title}",
            f"艺人：{artist}",
            f"原因：{error}",
            "已有歌词文件（如有）保持原样，音频文件未被修改。",
            f"后台任务：#{notification.job_id}",
        ]
    )


def word_lyrics_notification_keyboard(
    notification: WordLyricNotification,
) -> dict[str, Any] | None:
    if notification.terminal_status != "awaiting_mac_consent":
        return None
    return {
        "inline_keyboard": [
            [
                {
                    "text": "授权并调用 Mac",
                    "callback_data": (
                        f"{WORD_LYRICS_MAC_CALLBACK_PREFIX}:approve:{notification.job_id}"
                    ),
                }
            ],
            [
                {
                    "text": "不调用，保留现状",
                    "callback_data": (
                        f"{WORD_LYRICS_MAC_CALLBACK_PREFIX}:decline:{notification.job_id}"
                    ),
                }
            ],
        ]
    }


def deliver_word_lyrics_notification(store: NotificationStore, notification: WordLyricNotification) -> bool:
    result = send_message(str(notification.context["chat_id"]),
        format_word_lyrics_notification(notification), word_lyrics_notification_keyboard(notification))
    message_id = str((result or {}).get("message_id") or "")
    if not message_id.isdigit() or int(message_id) <= 0:
        store.failed(notification.job_id, notification.lease_token, "telegram_send_failed")
        return False
    sent_chat = str((result.get("chat") or {}).get("id") or notification.context["chat_id"])
    store.delivered(notification.job_id, notification.lease_token,
        message_id=message_id, chat_id=sent_chat)
    log("[word-lyrics-notification] delivered "
        f"job_id={notification.job_id} status={notification.terminal_status}")
    return True


def word_lyrics_notification_loop() -> None:
    global word_lyrics_notification_heartbeat
    database_path = STATE_DIR / "word-lyrics" / "automation" / "queue.sqlite3"
    owner = f"telegram-bot:{socket.gethostname()}:{os.getpid()}"
    recovered_batches = False

    def queue_send(chat_id, text):
        global word_lyrics_notification_heartbeat
        word_lyrics_notification_heartbeat = time.monotonic()
        return send_message(chat_id, text)

    while not stop_event.is_set():
        word_lyrics_notification_heartbeat = time.monotonic()
        notification: WordLyricNotification | None = None
        try:
            store = NotificationStore(database_path)
            messages = LyricMessageStore(database_path)
            if not recovered_batches:
                messages.recover_interrupted(store, before=WORD_LYRICS_BOT_STARTED_AT,
                    enqueue=lambda path, context: enqueue_completion_job(path,
                        notification_context=context, require_notification=True))
                recovered_batches = True
            messages.reconcile(store, send=queue_send, edit=edit_message_text, delete=delete_message)
            word_lyrics_notification_heartbeat = time.monotonic()
            notification = store.claim(owner=owner)
            if notification is None:
                stop_event.wait(WORD_LYRICS_NOTIFICATION_POLL_SECONDS)
                continue
            deliver_word_lyrics_notification(store, notification)
        except Exception as exc:
            log("[word-lyrics-notification] delivery loop error "
                f"job_id={notification.job_id if notification else 0} "
                f"error={type(exc).__name__}:{redact_sensitive_text(exc)}")
            stop_event.wait(WORD_LYRICS_NOTIFICATION_POLL_SECONDS)


def edit_message_text(
    chat_id: str,
    message_id: str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    parse_mode_name: str | None = None,
) -> bool:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:3900],
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode_name:
        payload["parse_mode"] = parse_mode_name
    try:
        telegram_api("editMessageText", payload, timeout=30)
        return True
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return True
        log(f"failed to edit Telegram message: {exc}")
        return False


def delete_message(chat_id: str, message_id: str) -> bool:
    if not message_id:
        return False
    try:
        telegram_api(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
            timeout=30,
        )
        return True
    except Exception as exc:
        if "message to delete not found" in str(exc).lower():
            return True
        log(f"failed to delete Telegram message {message_id}: {redact_sensitive_text(exc)}")
        return False


def send_photo(
    chat_id: str,
    photo_url: str,
    caption: str,
    reply_markup: dict[str, Any] | None = None,
    parse_mode_name: str | None = None,
) -> bool:
    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption[:1024],
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode_name:
        payload["parse_mode"] = parse_mode_name
    last_error: Exception | None = None
    for attempt in range(1, TELEGRAM_SEND_ATTEMPTS + 1):
        try:
            telegram_api("sendPhoto", payload, timeout=30)
            return True
        except Exception as exc:
            last_error = exc
            if attempt >= TELEGRAM_SEND_ATTEMPTS:
                break
            log(
                "failed to send Telegram photo on attempt "
                f"{attempt}/{TELEGRAM_SEND_ATTEMPTS}: {exc}; retrying"
            )
            time.sleep(TELEGRAM_SEND_RETRY_DELAY_SECONDS * attempt)
    log(
        "failed to send Telegram photo after "
        f"{TELEGRAM_SEND_ATTEMPTS} attempt(s): {last_error}"
    )
    return False


def edit_message_caption(
    chat_id: str,
    message_id: str,
    caption: str,
    reply_markup: dict[str, Any] | None = None,
    parse_mode_name: str | None = None,
) -> bool:
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption[:1024],
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode_name:
        payload["parse_mode"] = parse_mode_name
    try:
        telegram_api("editMessageCaption", payload, timeout=30)
        return True
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return True
        log(f"failed to edit Telegram message caption: {exc}")
        return False


def edit_message_photo(
    chat_id: str,
    message_id: str,
    photo_url: str,
    caption: str,
    reply_markup: dict[str, Any] | None = None,
    parse_mode_name: str | None = None,
) -> bool:
    media: dict[str, Any] = {
        "type": "photo",
        "media": photo_url,
        "caption": caption[:1024],
    }
    if parse_mode_name:
        media["parse_mode"] = parse_mode_name
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "media": media,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        telegram_api("editMessageMedia", payload, timeout=30)
        return True
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return True
        log(f"failed to edit Telegram message photo: {exc}")
        return False


def answer_callback_query(
    callback_query_id: str,
    text: str = "",
    show_alert: bool = False,
) -> None:
    if not callback_query_id:
        return
    payload = {
        "callback_query_id": callback_query_id,
        "text": text[:180],
        "show_alert": show_alert,
    }
    try:
        telegram_api("answerCallbackQuery", payload, timeout=20)
    except Exception as exc:
        log(f"failed to answer Telegram callback query: {exc}")


def send_synology_message(text: str) -> None:
    if not SYN_CHAT_INCOMING_WEBHOOK_URL:
        log("Synology Chat incoming webhook URL is empty; skipped async reply")
        return
    payload = urllib.parse.urlencode(
        {"payload": json.dumps({"text": text[:3900]}, ensure_ascii=False)}
    ).encode("utf-8")
    request = urllib.request.Request(
        SYN_CHAT_INCOMING_WEBHOOK_URL,
        data=payload,
        headers={"content-type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with SYN_CHAT_REPLY_OPENER.open(request, timeout=20) as response:
            response.read()
    except Exception as exc:
        log(f"failed to send Synology Chat message: {exc}")


def send_reply(job: Job, text: str) -> None:
    if job.source == "telegram":
        # The download card owns download progress; the durable lyric observer
        # separately owns lyric queue cards and per-song result notifications.
        if job.task_id > 0:
            log(
                f"task {job.task_id} detail suppressed in favor of the live card: "
                f"{truncate_display_text(text, 180)}"
            )
            return
        send_message(job.chat_id, text)
    elif job.source == "synology":
        send_synology_message(text)


def load_offset() -> int | None:
    try:
        return int(json.loads(OFFSET_PATH.read_text(encoding="utf-8"))["offset"])
    except FileNotFoundError:
        return None
    except Exception as exc:
        log(f"failed to load offset, starting from latest unread update: {exc}")
        return None


def save_offset(offset: int) -> None:
    OFFSET_PATH.parent.mkdir(parents=True, exist_ok=True)
    OFFSET_PATH.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def append_journal(record: dict[str, Any]) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with journal_lock:
        with JOURNAL_PATH.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def job_record(job: Job) -> dict[str, Any]:
    return {
        "source": job.source,
        "chat_id": job.chat_id,
        "message_id": job.message_id,
        "urls": job.urls,
        "mode": job.mode,
        "kind": job.kind,
        "received_at": job.received_at,
        "task_id": job.task_id,
        "payload": job.payload,
    }


def task_card_title(job: Job, preferred: str = "") -> str:
    if preferred.strip():
        return truncate_display_text(preferred, 110)
    if not job.urls:
        return "等待读取媒体信息"
    if len(job.urls) > 1:
        return f"{len(job.urls)} 个链接"
    return "等待读取媒体信息"


def update_task_card_message(task_id: int, *, force: bool = False) -> bool:
    if task_id <= 0:
        return False
    now = time.monotonic()
    with task_card_update_lock:
        # Read the task only after taking the lock. The enqueue thread and the
        # worker can both request the first card at nearly the same time; a
        # pre-lock read lets both retain a stale empty status_message_id and
        # send duplicate cards.
        task = task_card_store.get(task_id)
        if not task or task["source"] != "telegram":
            return False
        last_update = task_card_last_update.get(task_id, 0.0)
        if not force and now - last_update < TASK_CARD_UPDATE_SECONDS:
            return False
        text = format_task_card(task)
        keyboard = task_keyboard(task) or {"inline_keyboard": []}
        message_id = str(task.get("status_message_id") or "")
        if message_id:
            if edit_message_text(
                str(task["chat_id"]),
                message_id,
                text,
                keyboard,
            ):
                task_card_last_update[task_id] = now
                return True
            # A transient edit failure must not create a second status card.
            # Keep the existing message id and retry it on the next update.
            log(
                f"task card {task_id} edit failed for message {message_id}; "
                "preserving the existing card without sending a duplicate"
            )
            return False
        sent = send_message(str(task["chat_id"]), text, keyboard)
        if not sent:
            return False
        new_message_id = str(sent.get("message_id") or "")
        if new_message_id:
            task_card_store.update(task_id, status_message_id=new_message_id)
        task_card_last_update[task_id] = now
        return True


def update_task_card(task_id: int, *, force: bool = False, **values: Any) -> None:
    if task_id <= 0:
        return
    task_card_store.update(task_id, **values)
    update_task_card_message(task_id, force=force)


def publish_task_completion(task_id: int) -> bool:
    if task_id <= 0:
        return False
    task = task_card_store.get(task_id)
    if not task or task["source"] != "telegram":
        return False
    previous_message_id = str(task.get("status_message_id") or "")
    sent: dict[str, Any] | None = None
    with task_card_update_lock:
        sent = send_message(
            str(task["chat_id"]),
            format_task_card(task),
            task_keyboard(task) or {"inline_keyboard": []},
        )
        if sent:
            new_message_id = str(sent.get("message_id") or "")
            if new_message_id:
                task_card_store.update(task_id, status_message_id=new_message_id)
                task_card_last_update[task_id] = time.monotonic()
                if previous_message_id and previous_message_id != new_message_id:
                    delete_message(str(task["chat_id"]), previous_message_id)
                return True
    log(f"could not publish completion card for task {task_id}; editing progress card")
    return update_task_card_message(task_id, force=True)


def complete_task_card(task_id: int, **values: Any) -> None:
    if task_id <= 0:
        return
    task_card_store.update(task_id, **values)
    publish_task_completion(task_id)


def task_status(job: Job) -> str:
    if job.task_id <= 0:
        return ""
    task = task_card_store.get(job.task_id)
    return str(task.get("status") or "") if task else ""


def set_active_task_process(job: Job, process: subprocess.Popen[str]) -> None:
    global active_task_process, active_task_process_id
    with task_process_lock:
        active_task_process = process
        active_task_process_id = job.task_id
    status = task_status(job)
    if status == "paused":
        signal_task_process(job.task_id, signal.SIGSTOP)
    elif status in {"stopping", "stopped"}:
        signal_task_process(job.task_id, signal.SIGTERM)


def clear_active_task_process(
    job: Job,
    process: subprocess.Popen[str] | None = None,
) -> None:
    global active_task_process, active_task_process_id
    with task_process_lock:
        if active_task_process_id != job.task_id:
            return
        if process is not None and active_task_process is not process:
            return
        active_task_process = None
        active_task_process_id = 0


def signal_task_process(task_id: int, signum: int) -> bool:
    with task_process_lock:
        process = active_task_process if active_task_process_id == task_id else None
    if process is None or process.poll() is not None:
        return False
    try:
        os.killpg(process.pid, signum)
    except (OSError, ProcessLookupError):
        try:
            process.send_signal(signum)
        except (OSError, ProcessLookupError):
            return False
    return True


def task_should_stop(job: Job) -> bool:
    return task_status(job) in {"stopping", "stopped"}


def wait_for_task_ready(job: Job) -> bool:
    while not stop_event.is_set():
        status = task_status(job)
        if status in {"stopping", "stopped"}:
            return False
        if status != "paused":
            return True
        time.sleep(0.5)
    return False


def preserve_job_for_restart(job: Job) -> bool:
    """Service shutdown is not user cancellation or successful completion."""
    if not stop_event.is_set() or task_should_stop(job):
        return False
    status = task_status(job)
    if status in {"completed", "failed", "deleted"}:
        return False
    if job.task_id > 0:
        task_card_store.update(
            job.task_id,
            status="paused" if status == "paused" else "queued",
            phase="服务重启后继续",
            finished_at=None,
            speed_bps=None,
            eta=None,
        )
    append_journal({"event": "deferred_for_restart", "job": job_record(job), "time": time.time()})
    return True


def unique_path_strings(paths: list[str | Path]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for path in paths:
        text = str(path)
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def deletion_targets_for_media(kind: str, paths: list[str | Path]) -> list[str]:
    targets: list[str | Path] = []
    for value in paths:
        path = Path(value)
        if kind in {"youtube_video", "bilibili_video"}:
            targets.append(path.parent)
            continue
        targets.append(path)
        for suffix in (".lrc", ".ttml", ".srt"):
            sidecar = path.with_suffix(suffix)
            if sidecar.exists():
                targets.append(sidecar)
    return unique_path_strings(targets)


def allowed_delete_roots(task: dict[str, Any]) -> list[Path]:
    kind = str(task["kind"])
    mode = str(task["mode"])
    if kind == "apple_music":
        if mode == "both":
            return [ALAC_OUTPUT_PATH, ATMOS_OUTPUT_PATH, LX_FALLBACK_OUTPUT_PATH]
        roots = [output_for_mode(mode)]
        if mode == "alac":
            roots.extend([LX_FALLBACK_OUTPUT_PATH, SQ_MUSIC_OUTPUT_PATH])
        return roots
    if kind == "lx_music":
        return [LX_FALLBACK_OUTPUT_PATH]
    if kind == "youtube_audio":
        return [YOUTUBE_AUDIO_ROOT]
    if kind == "youtube_video":
        return [YOUTUBE_VIDEO_ROOT]
    if kind == "bilibili_audio":
        return [BILIBILI_AUDIO_ROOT]
    if kind == "bilibili_video":
        return [BILIBILI_VIDEO_ROOT]
    return []


def delete_task_files(task: dict[str, Any]) -> str:
    online_media = str(task["kind"]) in {
        "youtube_audio", "youtube_video", "bilibili_audio", "bilibili_video",
    }
    if not online_media:
        return delete_task_files_locked(task)
    # Do not block Telegram polling behind a potentially long download.
    if not online_video_library_lock.acquire(blocking=False):
        return "正在处理 YouTube / 哔哩哔哩媒体，请在当前下载完成后再删除。"
    try:
        return delete_task_files_locked(task)
    finally:
        online_video_library_lock.release()


def delete_task_files_locked(task: dict[str, Any]) -> str:
    task_id = int(task["id"])
    if str(task["status"]) in TASK_CARD_ACTIVE_STATUSES:
        return "请先停止任务，再删除文件。"
    candidates = json_paths(task.get("delete_paths_json"))
    deleted, rejected = delete_recorded_paths(
        candidates,
        allowed_delete_roots(task),
    )
    if rejected:
        return "部分文件删除失败：" + "；".join(rejected[:5])
    kind = str(task["kind"])
    if kind.startswith("youtube_"):
        history = (YOUTUBE_DB_PATH, YOUTUBE_STATE_DIR, "video_id", "youtube")
    elif kind.startswith("bilibili_"):
        history = (BILIBILI_DB_PATH, BILIBILI_STATE_DIR, "media_id", "bilibili")
    else:
        history = None
    if history is not None:
        database_path, state_dir, id_column, archive_prefix = history
        try:
            remove_deleted_media_history(
                database_path=database_path,
                kind=kind,
                id_column=id_column,
                archive_path=state_dir / ("archive-video.txt" if kind.endswith("_video") else "archive-audio.txt"),
                archive_prefix=archive_prefix,
                paths=candidates,
            )
        except (OSError, sqlite3.Error) as exc:
            log(f"task {task_id} download history cleanup failed: {exc}")
            return "文件已删除，但下载记录清理未完成；请稍后再次点击删除以完成清理。"
    task_card_store.update(
        task_id,
        status="deleted",
        phase="文件已删除",
        local_paths_json="[]",
        delete_paths_json="[]",
        speed_bps=None,
        eta=None,
        error="",
    )
    kind = str(task["kind"])
    if kind in {"youtube_video", "bilibili_video"}:
        trigger_emby_scan()
    else:
        trigger_navidrome_scan()
    if not deleted:
        return "任务记录中没有仍然存在的本地文件。"
    return "已删除：" + "、".join(deleted[:10])


def online_process_observer(
    job: Job,
    process: subprocess.Popen[str] | None,
) -> None:
    if process is None:
        clear_active_task_process(job)
    else:
        set_active_task_process(job, process)


def online_progress_observer(job: Job, progress: dict[str, Any]) -> None:
    if job.task_id <= 0:
        return
    task = task_card_store.get(job.task_id)
    if task is None or str(task["status"]) in {"stopping", "stopped"}:
        return
    values: dict[str, Any] = {}
    for key in (
        "phase",
        "title",
        "percent",
        "downloaded_bytes",
        "total_bytes",
        "total_estimated",
        "speed_bps",
        "eta",
    ):
        if key in progress:
            values[key] = progress[key]
    if str(task["status"]) != "paused":
        values["status"] = "running"
    if values:
        task_card_store.update(job.task_id, **values)
    update_task_card_message(job.task_id)


def sync_current_task_card(job: Job) -> None:
    if job.task_id <= 0:
        return
    values: dict[str, Any] = {}
    with download_progress_lock:
        progress = current_download_progress
    if progress is not None and progress.job_label == job.label:
        values["phase"] = progress.phase
        if progress.title:
            values["title"] = progress.title
        if progress.percent is not None:
            values["percent"] = progress.percent
        if progress.total_bytes is not None:
            values["total_bytes"] = int(progress.total_bytes)
            values["total_estimated"] = int(progress.total_estimated)
            if progress.percent is not None:
                values["downloaded_bytes"] = int(
                    progress.total_bytes * progress.percent / 100
                )
        if progress.speed_bytes_per_second is not None:
            values["speed_bps"] = progress.speed_bytes_per_second
        if progress.eta:
            values["eta"] = progress.eta
    if values:
        task_card_store.update(job.task_id, **values)
    update_task_card_message(job.task_id)


def task_card_loop() -> None:
    while not stop_event.is_set():
        with current_job_lock:
            job = current_job
        if job is not None:
            try:
                sync_current_task_card(job)
            except Exception as exc:
                log(f"task card update failed for task {job.task_id}: {exc}")
        stop_event.wait(1)


def handle_task_card_callback(callback_query: dict[str, Any]) -> None:
    callback_id = str(callback_query.get("id") or "")
    data = str(callback_query.get("data") or "")
    parts = data.split(":")
    if len(parts) != 3:
        answer_callback_query(callback_id, "无效的任务操作。", show_alert=True)
        return
    _prefix, action, task_id_text = parts
    try:
        task_id = int(task_id_text)
    except ValueError:
        answer_callback_query(callback_id, "无效的任务编号。", show_alert=True)
        return
    task = task_card_store.get(task_id)
    if not task:
        answer_callback_query(callback_id, "任务不存在。", show_alert=True)
        return
    message = callback_query.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id") or "")
    message_id = str(message.get("message_id") or task.get("status_message_id") or "")
    if not chat_allowed(chat_id) or chat_id != str(task["chat_id"]):
        answer_callback_query(callback_id, "没有权限操作这个任务。", show_alert=True)
        return

    status = str(task["status"])
    result = ""
    if action == "delete":
        if status != "completed":
            answer_callback_query(callback_id, "只有已完成任务可以删除文件。", show_alert=True)
            return
        if not json_paths(task.get("delete_paths_json")):
            answer_callback_query(callback_id, "本任务没有可删除的新文件。", show_alert=True)
            return
        confirmation = (
            f"确认删除任务 #{task_id:06d} 本次新下载并写入媒体库的文件？\n\n"
            "只会删除该任务数据库中记录的新文件，不会删除下载前已经存在的内容。"
            "此操作不可撤销。"
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "确认删除",
                        "callback_data": f"task:confirm_delete:{task_id}",
                    },
                    {"text": "取消", "callback_data": f"task:detail:{task_id}"},
                ]
            ]
        }
        if not message_id or not edit_message_text(
            chat_id,
            message_id,
            confirmation,
            keyboard,
        ):
            answer_callback_query(callback_id, "无法显示删除确认，请稍后重试。", show_alert=True)
            return
        answer_callback_query(callback_id, "请再次确认是否删除。")
        return
    if action == "detail":
        if not message_id or not edit_message_text(
            chat_id,
            message_id,
            format_task_card(task),
            task_keyboard(task) or {"inline_keyboard": []},
        ):
            answer_callback_query(callback_id, "无法恢复任务卡片，请稍后重试。", show_alert=True)
            return
        answer_callback_query(callback_id, "已取消删除。")
        return
    if action == "confirm_delete":
        if status != "completed":
            answer_callback_query(callback_id, "任务状态已经改变，未执行删除。", show_alert=True)
            return
        result = delete_task_files(task)
        updated = task_card_store.get(task_id) or task
        if str(updated["status"]) == "deleted":
            text = (
                f"Apple Music Downloader 文件已删除 #{task_id:06d}\n"
                f"来源：{SOURCE_LABELS.get(str(task['kind']), str(task['kind']))}\n"
                f"结果：{result}"
            )
            keyboard = {"inline_keyboard": []}
        else:
            text = format_task_card(updated)
            keyboard = task_keyboard(updated) or {"inline_keyboard": []}
        if message_id:
            edit_message_text(chat_id, message_id, text, keyboard)
        answer_callback_query(
            callback_id,
            result,
            show_alert=str(updated["status"]) != "deleted",
        )
        return
    if action == "pause":
        if status == "queued":
            task_card_store.update(task_id, status="paused", phase="等待继续")
            result = "任务已暂停。"
        elif status == "running" and signal_task_process(task_id, signal.SIGSTOP):
            task_card_store.update(
                task_id,
                status="paused",
                phase="已暂停",
                speed_bps=None,
                eta=None,
            )
            result = "下载已暂停。"
        elif status == "running":
            with current_job_lock:
                is_current = current_job is not None and current_job.task_id == task_id
            if is_current:
                task_card_store.update(
                    task_id,
                    status="paused",
                    phase="等待继续",
                    speed_bps=None,
                    eta=None,
                )
                result = "任务将在当前准备步骤结束后暂停。"
            else:
                result = "当前阶段暂时不能暂停。"
        else:
            result = "当前阶段暂时不能暂停。"
    elif action == "resume":
        if status != "paused":
            result = "该任务不是暂停状态。"
        elif signal_task_process(task_id, signal.SIGCONT):
            task_card_store.update(task_id, status="running")
            result = "下载已继续。"
        else:
            task_card_store.update(task_id, status="queued", phase="等待下载")
            result = "任务已重新进入队列。"
    elif action == "stop":
        if status not in TASK_CARD_ACTIVE_STATUSES:
            result = "任务已经结束。"
        elif signal_task_process(task_id, signal.SIGCONT):
            task_card_store.update(
                task_id,
                status="stopping",
                phase="正在停止",
                speed_bps=None,
                eta=None,
            )
            signal_task_process(task_id, signal.SIGTERM)
            result = "停止指令已发送。"
        else:
            task_card_store.update(
                task_id,
                status="stopped",
                phase="已停止",
                finished_at=time.time(),
                speed_bps=None,
                eta=None,
            )
            result = "任务已停止。"
    else:
        result = "未知的任务操作。"
    update_task_card_message(task_id, force=True)
    answer_callback_query(callback_id, result, show_alert=True)


def extract_urls(text: str) -> list[str]:
    urls = []
    for match in APPLE_MUSIC_URL_RE.finditer(text):
        url = match.group(0).rstrip(TRAILING_PUNCTUATION)
        if url not in urls:
            urls.append(url)
    return urls


def normalize_apple_music_url(item: dict[str, Any]) -> str:
    raw_url = str(item.get("trackViewUrl") or item.get("collectionViewUrl") or "").strip()
    track_id = str(item.get("trackId") or "").strip()
    collection_id = str(item.get("collectionId") or "").strip()
    if raw_url:
        parsed = urllib.parse.urlparse(raw_url)
        host = (parsed.hostname or "").lower()
        if host.endswith("itunes.apple.com") or host.endswith("music.apple.com"):
            path = parsed.path or ""
            parts = [part for part in path.split("/") if part]
            if not parts or not STORE_FRONT_RE.fullmatch(parts[0]):
                path = "/" + SEARCH_COUNTRY.lower() + path
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if track_id:
                query["i"] = [track_id]
            clean_query = urllib.parse.urlencode(
                {
                    key: values[-1]
                    for key, values in query.items()
                    if key in {"i", "l"} and values
                }
            )
            return urllib.parse.urlunparse(("https", "music.apple.com", path, "", clean_query, ""))
    if collection_id and track_id:
        return f"https://music.apple.com/{SEARCH_COUNTRY.lower()}/album/{collection_id}?i={track_id}"
    return raw_url


def normalize_artwork_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    return re.sub(r"/\d+x\d+bb\.", "/600x600bb.", url)


def apple_music_search_request(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    internal: bool = False,
) -> dict[str, Any]:
    openers: list[tuple[str, urllib.request.OpenerDirector]] = []
    if SEARCH_PROXY_URL and not internal:
        openers.append(("proxy", SEARCH_OPENER))
    openers.append(("direct", DIRECT_OPENER))
    last_error: Exception | None = None
    payload: dict[str, Any] | None = None
    for opener_name, opener in openers:
        for attempt in range(1, SEARCH_ATTEMPTS + 1):
            request = urllib.request.Request(
                url,
                headers={
                    "user-agent": "AppleMusicIngest/1.0",
                    "accept": "application/json,text/plain,*/*",
                    "connection": "close",
                    **(headers or {}),
                },
            )
            try:
                with opener.open(request, timeout=SEARCH_TIMEOUT_SECONDS) as response:
                    payload = json.loads(read_response_text(response))
                if not isinstance(payload, dict) or payload.get("errors"):
                    raise ValueError("Apple Music 搜索接口返回了错误或无效数据")
                last_error = None
                break
            except Exception as exc:
                payload = None
                last_error = exc
                if attempt >= SEARCH_ATTEMPTS or not retryable_network_error(exc):
                    break
                log(
                    f"Apple Music search {opener_name} attempt "
                    f"{attempt}/{SEARCH_ATTEMPTS} failed: {exc}; retrying"
                )
                time.sleep(SEARCH_RETRY_DELAY_SECONDS * attempt)
        if payload is not None or (
            last_error is not None and not retryable_network_error(last_error)
        ):
            break
        if opener_name == "proxy":
            log("Apple Music search proxy path failed; trying direct path")
    if payload is None:
        assert last_error is not None
        raise last_error
    return payload


def apple_music_search_token() -> str:
    # Reuse the authenticated wrapper; never send its credentials via a proxy.
    payload = apple_music_search_request(WRAPPER_URL.rstrip("/") + "/me", internal=True)
    auth = payload.get("auth")
    auth = auth if isinstance(auth, dict) else {}
    token = auth.get("dev_token") or payload.get("dev_token")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Apple Music 搜索认证不可用，请检查 wrapper 登录状态")
    return token.strip()


def apple_music_search(query: str) -> list[SearchResult]:
    # iTunes Store search returns zero songs for CN even when the Apple Music
    # subscription catalog has matches. Search the catalog in the chosen region.
    language = {
        "zh_cn": "zh-Hans-CN",
        "zh_tw": "zh-Hant-TW",
        "en_us": "en-US",
        "ja_jp": "ja-JP",
    }.get(SEARCH_LANGUAGE.lower(), SEARCH_LANGUAGE.replace("_", "-"))
    params = {
        "term": query,
        "types": "songs",
        "limit": str(min(25, SEARCH_LIMIT)),
        "l": language,
    }
    endpoint = f"https://api.music.apple.com/v1/catalog/{SEARCH_COUNTRY.lower()}/search?"
    token = apple_music_search_token()
    refreshed = False
    catalog_items: list[Any] = []
    while len(catalog_items) < SEARCH_LIMIT:
        # The official catalog endpoint permits at most 25 songs per request.
        params["limit"] = str(min(25, SEARCH_LIMIT - len(catalog_items)))
        if catalog_items:
            params["offset"] = str(len(catalog_items))
        url = endpoint + urllib.parse.urlencode(params)
        while True:
            try:
                payload = apple_music_search_request(
                    url,
                    {
                        "Authorization": f"Bearer {token}",
                        "Origin": "https://music.apple.com",
                        "Referer": "https://music.apple.com/",
                    },
                )
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in {401, 403} or refreshed:
                    raise
                refreshed = True
                token = apple_music_search_token()

        catalog_results = payload.get("results")
        if not isinstance(catalog_results, dict):
            raise ValueError("Apple Music 搜索结果格式异常，请稍后重试")
        songs = catalog_results.get("songs", {"data": []})
        if not isinstance(songs, dict) or not isinstance(songs.get("data"), list):
            raise ValueError("Apple Music 歌曲列表格式异常，请稍后重试")
        catalog_items.extend(songs["data"][:SEARCH_LIMIT - len(catalog_items)])
        if not songs["data"] or not songs.get("next"):
            break

    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in catalog_items:
        if not isinstance(item, dict):
            continue
        track_id = str(item.get("id") or "").strip()
        attributes = item.get("attributes")
        if (
            not track_id.isdigit()
            or item.get("type") != "songs"
            or not isinstance(attributes, dict)
            or not attributes.get("name")
            or track_id in seen
        ):
            continue
        seen.add(track_id)
        song_url = str(attributes.get("url") or "").strip()
        parsed_url = urllib.parse.urlparse(song_url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "music.apple.com"
            or apple_music_song_id_from_url(song_url) != track_id
        ):
            song_url = f"https://music.apple.com/{SEARCH_COUNTRY.lower()}/song/{track_id}"
        release_date = str(attributes.get("releaseDate") or "")
        artwork = attributes.get("artwork")
        artwork = artwork if isinstance(artwork, dict) else {}
        artwork_url = str(artwork.get("url") or "")
        artwork_url = artwork_url.replace("{w}", "600").replace("{h}", "600")
        genres = attributes.get("genreNames")
        genres = genres if isinstance(genres, list) else []
        results.append(
            SearchResult(
                title=str(attributes["name"]).strip(),
                artist=str(attributes.get("artistName") or "未知艺人").strip(),
                album=str(attributes.get("albumName") or "未知专辑").strip(),
                url=song_url,
                track_id=track_id,
                release_year=release_date[:4] if len(release_date) >= 4 else "",
                genre=str(genres[0]) if genres else "",
                artwork_url=artwork_url,
                duration_seconds=(float(attributes["durationInMillis"]) / 1000 if attributes.get("durationInMillis") else None),
                isrc=str(attributes.get("isrc") or ""),
            )
        )
    if catalog_items and not results:
        raise ValueError("Apple Music 返回的歌曲数据无法解析，请稍后重试")
    log(f"Apple Music catalog search country={SEARCH_COUNTRY} query={query!r} results={len(results)}")
    return results


def read_apple_failure_text(log_file: Path, tail: list[str]) -> str:
    chunks = ["\n".join(tail)]
    try:
        with log_file.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 262_144), os.SEEK_SET)
            chunks.append(handle.read().decode("utf-8", errors="replace"))
    except OSError:
        pass
    return "\n".join(chunks)


def itunes_lookup_track_for_lx(apple_music_url: str) -> AppleTrack:
    track_id = apple_music_song_id_from_url(apple_music_url)
    if not track_id:
        raise LxFallbackError("Apple Music link does not identify one song")
    storefront = storefront_from_url(apple_music_url)
    requested_country = (
        storefront.split("-", 1)[0].upper() if storefront else SEARCH_COUNTRY
    )
    lookup_countries = tuple(
        dict.fromkeys((*LX_FALLBACK_METADATA_COUNTRIES, requested_country))
    )
    openers: list[tuple[str, urllib.request.OpenerDirector]] = []
    if SEARCH_PROXY_URL:
        openers.append(("proxy", SEARCH_OPENER))
    openers.append(("direct", DIRECT_OPENER))
    last_error: Exception | None = None
    successful_countries: list[str] = []
    for lookup_country in lookup_countries:
        params = {
            "id": track_id,
            "country": lookup_country,
            "entity": "song",
            "lang": SEARCH_LANGUAGE,
        }
        url = "https://itunes.apple.com/lookup?" + urllib.parse.urlencode(params)
        payload: dict[str, Any] | None = None
        country_error: Exception | None = None
        for opener_name, opener in openers:
            for attempt in range(1, SEARCH_ATTEMPTS + 1):
                request = urllib.request.Request(
                    url,
                    headers={
                        "user-agent": "AppleMusicIngest/1.0",
                        "accept": "application/json,text/plain,*/*",
                        "connection": "close",
                    },
                )
                try:
                    with opener.open(
                        request,
                        timeout=SEARCH_TIMEOUT_SECONDS,
                    ) as response:
                        payload = json.loads(read_response_text(response))
                    country_error = None
                    break
                except Exception as exc:
                    country_error = exc
                    last_error = exc
                    if (
                        attempt >= SEARCH_ATTEMPTS
                        or not retryable_network_error(exc)
                    ):
                        break
                    log(
                        f"Apple Music lookup {opener_name} attempt "
                        f"{attempt}/{SEARCH_ATTEMPTS} failed track_id={track_id} "
                        f"country={lookup_country}: {type(exc).__name__}; retrying"
                    )
                    time.sleep(SEARCH_RETRY_DELAY_SECONDS * attempt)
            if payload is not None or (
                country_error is not None
                and not retryable_network_error(country_error)
            ):
                break
        if payload is None:
            continue
        successful_countries.append(lookup_country)

        item = next(
            (
                result
                for result in payload.get("results") or []
                if isinstance(result, dict)
                and str(result.get("trackId") or "") == str(track_id)
            ),
            None,
        )
        if item is None:
            continue
        title = str(item.get("trackName") or "").strip()
        artist = str(item.get("artistName") or "").strip()
        if not title or not artist:
            continue
        duration_seconds: float | None = None
        try:
            duration_ms = float(item.get("trackTimeMillis") or 0)
            if duration_ms > 0:
                duration_seconds = duration_ms / 1000
        except (TypeError, ValueError):
            pass
        log(
            f"Apple metadata fallback selected country={lookup_country} "
            f"track_id={track_id} title={title!r} artist={artist!r}"
        )
        return AppleTrack(
            track_id=str(track_id),
            title=title,
            artist=artist,
            album=str(item.get("collectionName") or "").strip(),
            duration_seconds=duration_seconds,
        )

    if not successful_countries:
        raise LxFallbackError(
            "Apple metadata lookup failed: "
            f"{type(last_error).__name__ if last_error else 'unknown error'}"
        )
    raise LxFallbackError(
        "Apple metadata lookup returned no matching song in "
        + ",".join(successful_countries)
    )


def update_lx_fallback_progress(job: Job, task: dict[str, Any]) -> None:
    global current_download_progress
    status = str(task.get("status") or "")
    quality = str(task.get("quality") or "").strip().casefold()
    media_label = "320k MP3" if quality == MP3_320_QUALITY else "FLAC"
    phase = {
        "waiting": f"等待 LX {media_label} 下载",
        "downloading": f"LX {media_label} 下载中",
        "tagging": f"LX {media_label} 写入标签",
        "finished": f"LX {media_label} 下载完成",
        "exists": f"LX {media_label} 已存在",
    }.get(status, "LX 高品质音源兜底中")
    try:
        total = max(0, int(float(task.get("total") or 0)))
        received = max(0, int(float(task.get("received") or 0)))
        speed = max(0.0, float(task.get("speed") or 0))
        raw_percent = float(task.get("progress") or 0)
    except (TypeError, ValueError):
        total = received = 0
        speed = raw_percent = 0.0
    percent = min(100.0, max(0.0, raw_percent))
    with download_progress_lock:
        progress = current_download_progress
        if progress is None or progress.job_label != job.label:
            return
        current_download_progress = replace(
            progress,
            phase=phase,
            updated_at=time.time(),
            title=str((task.get("songInfo") or {}).get("name") or progress.title),
            percent=percent,
            total_bytes=total or None,
            total_estimated=False,
            speed_bytes_per_second=speed or None,
            eta="",
        )


def verify_lx_flac(
    path: Path,
    *,
    requested_quality: str = "flac",
) -> dict[str, int | float | str]:
    try:
        stat = path.stat()
        with path.open("rb") as handle:
            header = handle.read(42)
    except OSError as exc:
        raise LxFallbackError("LX FLAC file is not readable from the Apple Music container") from exc
    if not path.is_file() or stat.st_size < 65_536 or header[:4] != b"fLaC":
        raise LxFallbackError("LX fallback output failed the FLAC file check")
    if len(header) < 42 or header[4] & 0x7F != 0:
        raise LxFallbackError("LX FLAC output has no valid STREAMINFO block")
    streaminfo_length = int.from_bytes(header[5:8], "big")
    if streaminfo_length < 34:
        raise LxFallbackError("LX FLAC STREAMINFO block is incomplete")
    packed = int.from_bytes(header[18:26], "big")
    sample_rate = (packed >> 44) & 0xFFFFF
    channels = ((packed >> 41) & 0x07) + 1
    bit_depth = ((packed >> 36) & 0x1F) + 1
    total_samples = packed & ((1 << 36) - 1)
    if sample_rate <= 0 or channels <= 0 or bit_depth <= 0 or total_samples <= 0:
        raise LxFallbackError("LX FLAC STREAMINFO contains invalid audio properties")
    if (
        requested_quality in {"master", "hires", "flac24bit"}
        and bit_depth <= 16
        and sample_rate <= 48_000
    ):
        raise LxFallbackError(
            f"LX source downgraded {requested_quality} to {bit_depth}bit/{sample_rate}Hz"
        )
    try:
        decoded = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(path),
                "-f",
                "null",
                "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LxFallbackError("LX FLAC full decode verification could not run") from exc
    if decoded.returncode != 0:
        detail = truncate_display_text(decoded.stderr or "decode failed", 300)
        raise LxFallbackError(f"LX FLAC full decode verification failed: {detail}")
    duration = total_samples / sample_rate
    bitrate_kbps = (stat.st_size * 8 / duration / 1000) if duration > 0 else 0
    return {
        "codec": "flac",
        "sample_rate": sample_rate,
        "bit_depth": bit_depth,
        "channels": channels,
        "duration_seconds": round(duration, 3),
        "bitrate_kbps": round(bitrate_kbps),
        "size_bytes": stat.st_size,
    }


def verify_lx_mp3_320(path: Path) -> dict[str, int | float | str]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise LxFallbackError("LX 320k MP3 is not readable from the Apple Music container") from exc
    if not path.is_file() or stat.st_size < 65_536 or path.suffix.casefold() != ".mp3":
        raise LxFallbackError("LX 320k fallback output failed the MP3 file check")
    try:
        probed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels,bit_rate:format=format_name,duration,bit_rate,size",
                "-of",
                "json",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LxFallbackError("LX 320k MP3 verification could not run ffprobe") from exc
    if probed.returncode != 0:
        detail = truncate_display_text(probed.stderr or "probe failed", 300)
        raise LxFallbackError(f"LX 320k MP3 probe failed: {detail}")
    try:
        payload = json.loads(probed.stdout)
        stream = (payload.get("streams") or [])[0]
        format_info = payload.get("format") or {}
        codec = str(stream.get("codec_name") or "").casefold()
        format_name = str(format_info.get("format_name") or "").casefold()
        sample_rate = int(stream.get("sample_rate") or 0)
        channels = int(stream.get("channels") or 0)
        stream_bitrate = int(stream.get("bit_rate") or 0)
        format_bitrate = int(format_info.get("bit_rate") or 0)
        duration = float(format_info.get("duration") or 0)
    except (IndexError, TypeError, ValueError, AttributeError, json.JSONDecodeError) as exc:
        raise LxFallbackError("LX 320k MP3 probe returned invalid audio properties") from exc
    if codec != "mp3" or "mp3" not in format_name:
        raise LxFallbackError(f"LX source mislabeled {codec or format_name or 'unknown'} as 320k MP3")
    bitrate = stream_bitrate or format_bitrate
    if bitrate < 300_000:
        raise LxFallbackError(
            f"LX source downgraded 320k MP3 to {round(bitrate / 1000)} kbps"
        )
    if sample_rate < 44_100 or channels < 2 or duration <= 30:
        raise LxFallbackError(
            "LX 320k MP3 has invalid audio properties: "
            f"{sample_rate}Hz/{channels}ch/{duration:.1f}s"
        )
    try:
        decoded = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(path),
                "-f",
                "null",
                "-",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LxFallbackError("LX 320k MP3 full decode verification could not run") from exc
    if decoded.returncode != 0:
        detail = truncate_display_text(decoded.stderr or "decode failed", 300)
        raise LxFallbackError(f"LX 320k MP3 full decode verification failed: {detail}")
    return {
        "codec": "mp3",
        "sample_rate": sample_rate,
        "bit_depth": 0,
        "channels": channels,
        "duration_seconds": round(duration, 3),
        "bitrate_kbps": round(bitrate / 1000),
        "size_bytes": stat.st_size,
    }


def verify_lx_audio(
    path: Path,
    *,
    requested_quality: str,
) -> dict[str, int | float | str]:
    if requested_quality.casefold() == MP3_320_QUALITY:
        return verify_lx_mp3_320(path)
    if not is_lossless_quality(requested_quality):
        raise LxFallbackError(f"unsupported LX quality: {requested_quality}")
    return verify_lx_flac(path, requested_quality=requested_quality)


def lx_audio_quality_description(
    quality: str,
    audio_info: dict[str, int | float | str],
) -> str:
    if quality.casefold() == MP3_320_QUALITY:
        return f"{int(audio_info['bitrate_kbps'])} kbps MP3"
    return (
        f"{int(audio_info['bit_depth'])}bit/"
        f"{int(audio_info['sample_rate']) / 1000:g}kHz FLAC"
    )


def ensure_lx_queue_kept_quality(
    task: dict[str, Any],
    requested_quality: str,
) -> None:
    actual_quality = str(task.get("quality") or "").strip().casefold()
    if actual_quality and actual_quality != requested_quality.casefold():
        raise LxFallbackError(
            f"LX source downgraded {requested_quality} to {actual_quality}"
        )


def lx_output_snapshot() -> dict[str, tuple[int, int, int, int]]:
    snapshot: dict[str, tuple[int, int, int, int]] = {}
    if not LX_FALLBACK_OUTPUT_PATH.exists():
        return snapshot
    for path in LX_FALLBACK_OUTPUT_PATH.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path)] = (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
        )
    return snapshot


def lx_path_existed_before(
    path: Path,
    before: dict[str, tuple[int, int, int, int]],
) -> bool:
    if str(path) in before:
        return True
    try:
        stat = path.stat()
    except OSError:
        return False
    signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    return signature in before.values()


def cleanup_rejected_lx_output(
    client: LxFallbackClient,
    candidate: Any,
    before: dict[str, tuple[int, int, int, int]],
) -> list[Path]:
    try:
        rejected_quality = str(getattr(candidate, "quality", "") or "").casefold()
        entry = client.cache_entry(
            candidate,
            quality=rejected_quality or None,
            exact_quality=bool(rejected_quality),
        )
        if entry is None or str(entry.get("folder") or "") != "music":
            return []
        path = map_server_music_path(
            entry.get("path"),
            LX_FALLBACK_OUTPUT_PATH,
            LX_FALLBACK_SERVER_MUSIC_ROOT,
        )
    except Exception as exc:
        log(f"could not locate rejected LX fallback output: {exc}")
        return []
    try:
        current = path.stat()
    except OSError:
        return []
    if lx_path_existed_before(path, before):
        log(
            "rejected LX fallback resolved to a pre-existing file; "
            "leaving the existing file untouched"
        )
        return []

    removed: list[Path] = []
    for target in (path, path.with_suffix(".lrc"), path.with_suffix(".ttml"), path.with_suffix(".srt")):
        if str(target) in before or not target.exists():
            continue
        try:
            target.unlink()
            removed.append(target)
        except OSError as exc:
            log(f"could not remove rejected LX fallback file {target.name}: {exc}")
    if removed:
        try:
            client.sync_index()
        except Exception as exc:
            log(f"LX index resync after rejected output cleanup failed: {exc}")
        log(
            f"removed {len(removed)} newly-created rejected LX fallback file(s); "
            f"audio_bytes={current.st_size}"
        )
    return removed


def lx_fallback_queue_id(
    job: Job,
    track_id: str,
    source: str,
    songmid: str,
    attempt: int,
) -> str:
    stable_job = str(job.task_id) if job.task_id > 0 else hashlib.sha256(
        f"{job.label}:{job.received_at}".encode("utf-8")
    ).hexdigest()[:16]
    candidate_key = hashlib.sha256(f"{source}:{songmid}".encode("utf-8")).hexdigest()[:10]
    value = re.sub(
        r"[^A-Za-z0-9_-]+",
        "_",
        f"amfb_{stable_job}_{track_id}_{attempt}_{candidate_key}",
    )
    return value[:160]


def apple_recording_candidate(result: SearchResult) -> RecordingCandidate:
    return RecordingCandidate(
        provider="apple", source="apple", source_id=result.track_id,
        title=result.title, artist=result.artist, album=result.album,
        duration_seconds=result.duration_seconds, isrc=result.isrc,
        url=result.url, artwork_url=result.artwork_url, quality="alac",
    )


def lx_recording_candidate(item: LxCandidate | LxSearchItem) -> RecordingCandidate:
    song_info = dict(item.song_info)
    return RecordingCandidate(
        provider="lx", source=item.source,
        source_id=str(song_info.get("songmid") or song_info.get("songId") or ""),
        title=item.title, artist=item.artist, album=item.album,
        duration_seconds=(item.duration_seconds if isinstance(item, LxCandidate)
                          else parse_duration_seconds(item.interval)),
        quality=item.quality, artwork_url=getattr(item, "artwork_url", ""),
        metadata=song_info,
    )


def sq_recording_candidate(item: Any) -> RecordingCandidate:
    return RecordingCandidate(
        provider="sq", source=item.source, source_id=str(item.song_id),
        title=item.title, artist=item.artist, album=item.album,
        duration_seconds=item.duration_seconds, quality=str(item.quality),
        artwork_url=item.artwork_url, metadata=dict(item.song_info),
    )


def sq_music_client() -> Any:
    from sq_music import SqMusicClient
    if not SQ_MUSIC_ENABLED or not SQ_MUSIC_AUTH_FILE:
        raise RuntimeError("SQ Music integration is not configured")
    auth = json.loads(Path(SQ_MUSIC_AUTH_FILE).read_text(encoding="utf-8"))
    username, password = str(auth.get("username") or ""), str(auth.get("password") or "")
    if not username or not password:
        raise RuntimeError("SQ Music credential file is incomplete")
    return SqMusicClient(
        SQ_MUSIC_BASE_URL, username, password,
        sources=SQ_MUSIC_SOURCES, staging_root=SQ_MUSIC_STAGING_PATH,
        sq_music_root=SQ_MUSIC_SERVER_ROOT,
    )


def unified_lx_search(query: str) -> list[LxSearchItem]:
    client = LxFallbackClient(
        LX_FALLBACK_BASE_URL, output_root=LX_FALLBACK_OUTPUT_PATH,
        frontend_auth_file=Path(LX_FALLBACK_AUTH_FILE) if LX_FALLBACK_AUTH_FILE else None,
        server_music_root=LX_FALLBACK_SERVER_MUSIC_ROOT,
        request_timeout_seconds=LX_FALLBACK_REQUEST_TIMEOUT_SECONDS,
        queue_timeout_seconds=LX_FALLBACK_QUEUE_TIMEOUT_SECONDS,
        poll_seconds=LX_FALLBACK_POLL_SECONDS, logger=log,
    )
    return client.search_flac(query, sources=LX_FALLBACK_SEARCH_SOURCES,
                              max_results=LX_SEARCH_MAX_RESULTS)


def unified_music_search(query: str) -> tuple[list[SearchResult], dict[str, str]]:
    searches = {"apple": lambda: apple_music_search(query)}
    if LX_FALLBACK_ENABLED:
        searches["lx"] = lambda: unified_lx_search(query)
    if SQ_MUSIC_ENABLED:
        searches["sq"] = lambda: sq_music_client().search_lossless(query, limit=20)
    found, errors = bounded_search(searches, timeout=UNIFIED_SEARCH_TIMEOUT_SECONDS)
    candidates = [apple_recording_candidate(item) for item in found.get("apple", [])]
    candidates.extend(lx_recording_candidate(item) for item in found.get("lx", [])
                      if is_lossless_quality(item.quality))
    candidates.extend(sq_recording_candidate(item) for item in found.get("sq", []))
    results = []
    for recording in merge_candidates(candidates):
        item = recording.primary
        results.append(SearchResult(
            title=item.title, artist=item.artist, album=item.album,
            url=item.url or f"unified://{item.provider}/{urllib.parse.quote(item.source_id, safe='')}",
            track_id=item.source_id, artwork_url=item.artwork_url,
            duration_seconds=item.duration_seconds, isrc=item.isrc,
            unified_payload=recording.to_payload(),
        ))
    if errors:
        log("unified music search partial sources=" + ",".join(sorted(errors)))
    return results, errors


def validate_recording_duration(recording: RecordingCandidate, info: dict[str, Any]) -> None:
    actual = float(info.get("duration_seconds") or 0)
    if actual <= 0:
        raise LxFallbackError("downloaded lossless audio has no valid duration")
    if recording.duration_seconds and abs(actual - recording.duration_seconds) > 2.0:
        raise LxFallbackError("downloaded audio duration does not match the selected recording")


def unified_apple_paths_for_song(paths: list[Path], apple_url: str) -> list[Path]:
    selected_id = apple_music_song_id_from_url(apple_url)
    matched = []
    root = ALAC_OUTPUT_PATH.resolve()
    for path in paths:
        if not path.resolve().is_relative_to(root):
            continue
        song_id, mode, _title, _isrc, _fingerprint = read_apple_media_identity(path)
        if selected_id and song_id == selected_id and mode == "alac":
            matched.append(path)
    return matched


def unified_download_phase(job: Job, phase: str) -> None:
    set_download_progress_phase(job, phase, reset_transfer=True)
    update_task_card(job.task_id, force=True, phase=phase, speed_bps=None, eta=None)


def verify_unified_apple_audio(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name,sample_rate,bits_per_raw_sample,duration:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=45, check=True,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    stream = streams[0] if streams else {}
    if stream.get("codec_name") != "alac":
        raise RuntimeError("Apple output did not pass the ALAC lossless check")
    duration = float(stream.get("duration") or (payload.get("format") or {}).get("duration") or 0)
    if duration <= 0 or int(stream.get("sample_rate") or 0) <= 0:
        raise RuntimeError("Apple output has invalid audio properties")
    decoded = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        timeout=900, check=False,
    )
    if decoded.returncode != 0:
        raise RuntimeError("Apple ALAC full decode verification failed")
    return {"codec": "alac", "duration_seconds": duration,
            "sample_rate": int(stream["sample_rate"]), "bit_depth": int(stream.get("bits_per_raw_sample") or 0)}


def unified_recording_for_apple_url(url: str) -> RecordingCandidate:
    track = itunes_lookup_track_for_lx(url)
    return RecordingCandidate("apple", "apple", track.track_id, track.title, track.artist,
                              track.album, track.duration_seconds, url=url, quality="alac")


def run_apple_with_retries(job: Job, mode_job: Job, mode: str) -> tuple[int, Path, list[str], list[Path]]:
    """The same recovery policy used by the established Apple-only pipeline."""
    retry_index = 0
    retry_proxy_url = ""
    while True:
        if not wait_for_task_ready(job):
            raise LxFallbackStopped("parent task stopped")
        result = run_download(mode_job, mode, proxy_url=retry_proxy_url)
        return_code, log_file, tail, _existing = result
        wrapper_failure = return_code == 72 and is_wrapper_m3u8_failure(log_file, tail)
        transient_failure = return_code == 72 and is_transient_gamdl_failure(log_file, tail)
        retry_limit = WRAPPER_M3U8_FAILURE_JOB_RETRIES if wrapper_failure else TRANSIENT_FAILURE_JOB_RETRIES
        if not (return_code == 72 and retry_index < retry_limit and (wrapper_failure or transient_failure)
                and not stop_event.is_set() and not task_should_stop(job)):
            return result
        retry_index += 1
        if wrapper_failure:
            recovered = recover_wrapper_m3u8(f"job={job.label} mode={mode}")
            delay = 5 if recovered else WRAPPER_M3U8_FAILURE_JOB_RETRY_DELAY_SECONDS
        else:
            delay = TRANSIENT_FAILURE_JOB_RETRY_DELAY_SECONDS
            if DOWNLOAD_PROXY_URL and not retry_proxy_url:
                retry_proxy_url = DOWNLOAD_PROXY_URL
        set_download_progress_phase(mode_job, f"Apple Music 等待自动重试 {retry_index}/{retry_limit}", reset_transfer=True)
        if stop_event.wait(delay) or task_should_stop(job):
            raise LxFallbackStopped("parent task stopped during retry")


def ensure_sq_publish_path(destination: Path) -> None:
    if SQ_MUSIC_OUTPUT_PATH.is_symlink():
        raise RuntimeError("SQ music root is a symbolic link")
    root = SQ_MUSIC_OUTPUT_PATH.resolve()
    if not destination.resolve().is_relative_to(root):
        raise RuntimeError("SQ publishing path escapes the music library")
    relative = destination.relative_to(SQ_MUSIC_OUTPUT_PATH)
    current = SQ_MUSIC_OUTPUT_PATH
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError("SQ publishing path contains a symbolic link")


def unified_owned_deletion_paths(job: Job, paths: list[Path], created: list[Path]) -> list[str]:
    # Retry/restart may reuse this task's earlier published file. Preserve only
    # previously recorded ownership that belongs to the currently accepted media.
    expected = set(deletion_targets_for_media("apple_music", paths))
    task = task_card_store.get(job.task_id) if job.task_id > 0 else None
    previous = json_paths(task.get("delete_paths_json")) if task else []
    retained = [str(path) for path in previous if str(path) in expected]
    return unique_path_strings(retained + deletion_targets_for_media("apple_music", created))


def publish_sq_lossless(job: Job, candidate: Any, recording: RecordingCandidate) -> tuple[Path, bool, dict[str, Any]]:
    from sq_music import SqMusicStopped
    client = sq_music_client()
    identity = hashlib.sha256(f"{candidate.source}:{candidate.song_id}".encode()).hexdigest()[:16]

    def safe(value: str) -> str:
        return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", value).strip(" .")[:100] or "Unknown"

    destination = SQ_MUSIC_OUTPUT_PATH / safe(recording.artist) / safe(recording.album or "Singles") / f"{safe(recording.title)} [{identity}].flac"
    ensure_sq_publish_path(destination)
    if destination.exists():
        info = verify_lx_flac(destination)
        validate_recording_duration(recording, info)
        return destination, False, info

    def stopped() -> bool:
        # Pause holds publishing and polling; cancellation stops this owned SQ task.
        return not wait_for_task_ready(job)

    try:
        staged = client.download_candidate(
            candidate, stop_requested=stopped,
            on_progress=lambda progress: unified_download_phase(job, f"SQ {candidate.source} 无损下载中"),
        )
    except SqMusicStopped as exc:
        raise LxFallbackStopped(str(exc)) from exc
    staged = Path(staged).resolve(strict=True)
    if not staged.is_relative_to(SQ_MUSIC_STAGING_PATH.resolve()):
        raise RuntimeError("SQ returned an output outside the private staging directory")
    info = verify_lx_flac(staged)
    validate_recording_duration(recording, info)
    if not wait_for_task_ready(job):
        raise LxFallbackStopped("parent stopped before SQ publishing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ensure_sq_publish_path(destination)
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(6)}.partial")
    created = False
    try:
        shutil.copy2(staged, temporary)
        copied_info = verify_lx_flac(temporary)
        validate_recording_duration(recording, copied_info)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        if not wait_for_task_ready(job):
            raise LxFallbackStopped("parent stopped before SQ publishing")
        ensure_sq_publish_path(destination)
        try:
            # Same-filesystem link is atomic and never replaces a pre-existing file.
            os.link(temporary, destination)
            created = True
        except FileExistsError:
            info = verify_lx_flac(destination)
            validate_recording_duration(recording, info)
    finally:
        temporary.unlink(missing_ok=True)
    return destination, created, info


def unified_sq_candidates(client: Any, target: RecordingCandidate) -> list[Any]:
    items = client.search_lossless(f"{target.artist} {target.title}", limit=30)
    sources = tuple(dict.fromkeys(SQ_MUSIC_SOURCES))
    grouped: dict[str, dict[str, Any]] = {source: {} for source in sources}

    def quality_rank(item: Any) -> int:
        value = str(item.quality).casefold()
        tail = value.rsplit("_", 1)[-1]
        return int(tail) if tail.isdigit() else (2800 if "res_" in value else 1411)

    for item in items:
        if (item.source not in grouped or not str(item.quality).casefold().startswith(item.source + "_flac_")
                or not strict_match(target, sq_recording_candidate(item))):
            continue
        previous = grouped[item.source].get(str(item.song_id))
        if previous is None or quality_rank(item) > quality_rank(previous):
            grouped[item.source][str(item.song_id)] = item
    # Bound retries while reserving room for each configured source. Each song
    # contributes its highest advertised FLAC tier only, never a lossy tier.
    per_source = max(1, 6 // max(1, len(sources)))
    candidates = []
    for source in sources:
        ranked = sorted(grouped[source].values(), key=quality_rank, reverse=True)
        candidates.extend(ranked[:per_source])
    return candidates[:6]


def process_unified_music_job(job: Job) -> None:
    """One durable parent, conservative Apple → LX → SQ lossless transitions."""
    paths: list[Path] = []
    created: list[Path] = []
    provider = ""
    phase = "准备统一无损下载"
    errors: list[str] = []
    target: RecordingCandidate | None = None
    begin_download_progress(job, "alac")
    try:
        payload = dict((job.payload or {}).get("unified_music") or {})
        apple_url = str(payload.get("apple_url") or "")
        recording = UnifiedRecording.from_payload(payload) if "candidates" in payload else None
        target = recording.primary if recording is not None else None
        apple_candidate = next((item for item in recording.candidates if item.provider == "apple"), None) if recording else None
        if apple_candidate is None and target is not None:
            unified_download_phase(job, "优先核对 Apple Music 同一录音")
            found, lookup_errors = bounded_search({"apple": lambda: apple_music_search(f"{target.artist} {target.title}")},
                                                 timeout=UNIFIED_SEARCH_TIMEOUT_SECONDS)
            if "apple" in lookup_errors:
                raise RuntimeError("Apple Music 优先核对暂时失败，请重试；尚未转用其他来源")
            apple_candidate = next((apple_recording_candidate(item) for item in found.get("apple", [])
                                    if strict_match(target, apple_recording_candidate(item))), None)
        if apple_candidate is not None:
            apple_url = apple_candidate.url
            target = apple_candidate
        if apple_url:
            if not apple_music_song_id_from_url(apple_url):
                raise ValueError("unified download requires one Apple Music song URL")
            apple_job = replace(job, urls=[apple_url])
            pending, existing = deduplicate_apple_music_urls([apple_url], "alac")
            if existing:
                paths = [Path(item.path) for item in existing]
                for path in paths:
                    info = verify_unified_apple_audio(path)
                    if target:
                        validate_recording_duration(target, info)
                provider, phase = "Apple Music", "Apple Music 无损已存在"
            else:
                before = media_snapshot(ALAC_OUTPUT_PATH)
                unified_download_phase(job, "Apple Music 无损下载中")
                code, logfile, tail, verified_existing = run_apple_with_retries(job, apple_job, "alac")
                if not wait_for_task_ready(job):
                    raise LxFallbackStopped("parent stopped after Apple download")
                after = media_snapshot(ALAC_OUTPUT_PATH)
                observed_changes = [Path(path) for path in changed_media_files(before, after)]
                changed = unified_apple_paths_for_song(observed_changes, apple_url)
                code = effective_download_return_code(code, changed_media_count=len(changed), verified_existing_count=len(verified_existing))
                if code == 0:
                    paths = changed + [Path(path) for path in verified_existing]
                    for path in paths:
                        info = verify_unified_apple_audio(path)
                        if target:
                            validate_recording_duration(target, info)
                    created = [path for path in paths if str(path) not in before]
                    provider, phase = "Apple Music", "Apple Music 无损下载完成"
                else:
                    signature = fallback_is_eligible(enabled=True, mode="alac", pending_url_count=1,
                        track_id=apple_music_song_id_from_url(apple_url) or "", return_code=code,
                        failure_text=read_apple_failure_text(logfile, tail),
                        allow_catalog_api_3076=LX_FALLBACK_ALLOW_CATALOG_API_3076)
                    if signature is None:
                        raise RuntimeError("Apple Music 下载故障，已保留原有重试策略；未按缺曲换源。" + f" 日志：{logfile}")
                    # Never hide a partial write or claim another source owns it.
                    unknown_new_outputs = [path for path in observed_changes
                        if str(path) not in before and path.suffix.lower() in {".m4a", ".mp4"}
                        and not read_apple_media_identity(path)[0]]
                    if changed or unknown_new_outputs:
                        raise RuntimeError("Apple Music 产生了待核对的文件，停止自动换源。" + f" 日志：{logfile}")
                    if target is None:
                        target = unified_recording_for_apple_url(apple_url)
                    errors.append("Apple Music 当前目录无可下载媒体")
        elif target is None:
            raise ValueError("unified task has no validated recording")
        else:
            errors.append("Apple Music 未找到严格匹配的同一录音")

        if not paths and LX_FALLBACK_ENABLED:
            assert target is not None
            unified_download_phase(job, "Apple Music 缺曲，尝试 LX 无损")
            fallback = run_lx_fallback(job, job, "alac", 66, Path("/nonexistent"), [],
                                       recording=target, unavailable_signature="unified_apple_unavailable")
            if fallback and fallback.success:
                paths, created = list(fallback.paths), list(fallback.created_paths)
                provider, phase = "LX", "LX 无损入库完成"
            else:
                errors.append("LX 未取得通过验收的无损音频")
        if not paths and SQ_MUSIC_ENABLED:
            assert target is not None
            if not wait_for_task_ready(job):
                raise LxFallbackStopped("parent stopped before SQ")
            unified_download_phase(job, "尝试 SQ 无损")
            client = sq_music_client()
            candidates = unified_sq_candidates(client, target)
            if not candidates:
                raise RuntimeError("SQ 没有严格匹配的同一录音无损候选")
            for index, candidate in enumerate(candidates, start=1):
                if not wait_for_task_ready(job):
                    raise LxFallbackStopped("parent stopped before SQ candidate")
                unified_download_phase(job, f"SQ 无损换源 {index}/{len(candidates)} · {candidate.source}")
                try:
                    path, was_created, _info = publish_sq_lossless(job, candidate, target)
                except LxFallbackStopped:
                    raise
                except Exception as exc:
                    if not wait_for_task_ready(job):
                        raise LxFallbackStopped("parent stopped after SQ candidate failure") from exc
                    failure = f"SQ {candidate.source}: {type(exc).__name__}"
                    errors.append(failure)
                    log(f"unified task={job.task_id} rejected candidate {index}/{len(candidates)}: {failure}")
                    continue
                paths, created = [path], [path] if was_created else []
                provider, phase = "SQ", "SQ 无损入库完成" if was_created else "SQ 无损已存在"
                break
            if not paths:
                raise RuntimeError(f"SQ {len(candidates)} 个无损候选均未通过下载验收")
        if not paths:
            raise RuntimeError("已启用的来源均未取得通过验收的无损音频")
        # Record ownership before lyrics/scan so restart and stop retain safe deletion.
        delete_paths = unified_owned_deletion_paths(job, paths, created)
        task_card_store.update(job.task_id, local_paths_json=json.dumps(unique_path_strings(paths), ensure_ascii=False),
                               delete_paths_json=json.dumps(delete_paths, ensure_ascii=False))
        if not wait_for_task_ready(job):
            raise LxFallbackStopped("parent stopped after publishing")
        if provider != "Apple Music":
            unified_download_phase(job, f"{provider} 无损已验收，处理歌词")
            try:
                sync_word_lyrics_for_paths(paths, source_url=apple_url,
                    notification_context={"source": job.source, "chat_id": job.chat_id,
                                          "message_id": job.message_id, "task_id": job.task_id}, logger=log)
            except Exception as exc:
                log(f"unified lyrics deferred task={job.task_id}: {redact_sensitive_text(exc)}")
            delete_paths = unified_owned_deletion_paths(job, paths, created)
        scan = trigger_navidrome_scan()
        if not wait_for_task_ready(job):
            raise LxFallbackStopped("parent stopped during library update")
        complete_task_card(job.task_id, status="completed", phase=phase,
            title=f"{target.artist} - {target.title}" if target else str((task_card_store.get(job.task_id) or {}).get("title") or "Apple Music 单曲"),
            finished_at=time.time(), percent=100.0, speed_bps=None, eta="00:00", error="" if scan else "Navidrome 将由定时扫描发现",
            local_paths_json=json.dumps(unique_path_strings(paths), ensure_ascii=False),
            delete_paths_json=json.dumps(delete_paths, ensure_ascii=False))
        append_journal({"event": "finished", "job": job_record(job), "time": time.time(),
                        "provider": provider, "local_paths": unique_path_strings(paths), "source_attempts": errors})
    except LxFallbackStopped:
        if not preserve_job_for_restart(job):
            update_task_card(job.task_id, force=True, status="stopped", phase="已停止", finished_at=time.time(), speed_bps=None, eta=None)
    except Exception as exc:
        if not preserve_job_for_restart(job):
            message = redact_sensitive_text(exc)
            update_task_card(job.task_id, force=True, status="failed", phase="统一无损下载未完成", finished_at=time.time(), speed_bps=None, eta=None,
                             error="；".join(errors + [message]))
            append_journal({"event": "failed", "job": job_record(job), "time": time.time(), "error": message, "source_attempts": errors})
    finally:
        clear_download_progress(job)


def run_lx_fallback(
    job: Job,
    mode_job: Job,
    mode: str,
    return_code: int,
    log_file: Path,
    tail: list[str],
    *,
    recording: RecordingCandidate | None = None,
    unavailable_signature: str = "",
) -> LxFallbackOutcome | None:
    apple_url = mode_job.urls[0] if len(mode_job.urls) == 1 else ""
    track_id = apple_music_song_id_from_url(apple_url) if apple_url else ""
    failure_text = read_apple_failure_text(log_file, tail)
    signature = unavailable_signature if recording is not None else fallback_is_eligible(
        enabled=LX_FALLBACK_ENABLED,
        mode=mode,
        pending_url_count=len(mode_job.urls),
        track_id=track_id or "",
        return_code=return_code,
        failure_text=failure_text,
        allow_catalog_api_3076=LX_FALLBACK_ALLOW_CATALOG_API_3076,
    )
    if signature is None:
        return None

    set_download_progress_phase(
        mode_job,
        "Apple Music 无可下载媒体，准备 LX 高品质音源兜底",
        reset_transfer=True,
    )
    update_task_card(
        job.task_id,
        force=True,
        phase="Apple Music 缺曲，准备 LX 高品质音源兜底",
        speed_bps=None,
        eta=None,
    )
    try:
        track = (AppleTrack(track_id=recording.source_id, title=recording.title, artist=recording.artist, album=recording.album, duration_seconds=recording.duration_seconds)
                 if recording is not None else itunes_lookup_track_for_lx(apple_url))
        if not LX_FALLBACK_ENABLED:
            raise LxFallbackError("LX integration is disabled")
        client = LxFallbackClient(
            LX_FALLBACK_BASE_URL,
            output_root=LX_FALLBACK_OUTPUT_PATH,
            frontend_auth_file=Path(LX_FALLBACK_AUTH_FILE) if LX_FALLBACK_AUTH_FILE else None,
            server_music_root=LX_FALLBACK_SERVER_MUSIC_ROOT,
            request_timeout_seconds=LX_FALLBACK_REQUEST_TIMEOUT_SECONDS,
            queue_timeout_seconds=LX_FALLBACK_QUEUE_TIMEOUT_SECONDS,
            poll_seconds=LX_FALLBACK_POLL_SECONDS,
            logger=log,
        )
        candidates = client.find_download_candidates(
            track,
            sources=LX_FALLBACK_SEARCH_SOURCES,
            allow_320=False if recording is not None else LX_FALLBACK_ALLOW_320,
        )[:LX_FALLBACK_MAX_CANDIDATES]
        if recording is not None:
            candidates = [candidate for candidate in candidates if is_lossless_quality(candidate.quality) and strict_match(recording, lx_recording_candidate(candidate))]
        for cached_candidate in candidates:
            try:
                cached_path = client.cached_path(
                    cached_candidate,
                    quality=cached_candidate.quality,
                )
                cached_audio_info = verify_lx_audio(
                    cached_path,
                    requested_quality=cached_candidate.quality,
                )
                if recording is not None:
                    validate_recording_duration(recording, cached_audio_info)
                cached_path = client.organize_cached_path(
                    cached_candidate,
                    quality=cached_candidate.quality,
                )
            except Exception:
                continue
            log(
                f"job {job.label} Apple failure={signature}; "
                "reusing verified LX cache "
                f"{cached_candidate.source}/{cached_candidate.quality}:"
                f"{cached_candidate.artist} - {cached_candidate.title}"
            )
            if recording is None:
                sync_word_lyrics_for_paths([cached_path], source_url=job.urls[0] if job.urls else "",
                    notification_context={"source": job.source, "chat_id": job.chat_id,
                        "message_id": job.message_id, "task_id": job.task_id}, logger=log)
            scan_triggered = trigger_navidrome_scan()
            set_download_progress_phase(
                mode_job,
                f"LX {lx_quality_label(cached_candidate.quality)} 缓存复用完成",
                reset_transfer=True,
            )
            return LxFallbackOutcome(
                success=True,
                signature=signature,
                message=(
                    f"Verified LX {lx_quality_label(cached_candidate.quality)} "
                    "cache reused"
                ),
                title=cached_candidate.title,
                artist=cached_candidate.artist,
                album=cached_candidate.album,
                paths=(cached_path,),
                scan_triggered=scan_triggered,
                quality=cached_candidate.quality,
                sample_rate=int(cached_audio_info["sample_rate"]),
                bit_depth=int(cached_audio_info["bit_depth"]),
            )
        attempt_errors: list[str] = []
        for attempt, candidate in enumerate(candidates, start=1):
            if stop_event.is_set() or task_should_stop(job):
                raise LxFallbackStopped("LX fallback stopped with the parent task")
            songmid = str(
                candidate.song_info.get("songmid")
                or candidate.song_info.get("songId")
                or ""
            )
            queue_id = lx_fallback_queue_id(
                job,
                track.track_id,
                candidate.source,
                songmid,
                attempt,
            )
            before = lx_output_snapshot()
            cleanup_candidate = candidate
            accepted_candidate = candidate
            log(
                f"job {job.label} Apple failure={signature}; LX fallback candidate "
                f"{attempt}/{len(candidates)}={candidate.source}/{candidate.quality}:"
                f"{candidate.artist} - {candidate.title}"
            )
            update_task_card(
                job.task_id,
                force=True,
                phase=(
                    f"LX {lx_quality_label(candidate.quality)} 兜底 "
                    f"{attempt}/{len(candidates)}"
                ),
                title=f"{candidate.artist} - {candidate.title}",
                speed_bps=None,
                eta=None,
            )
            try:
                queue_task = client.enqueue_and_wait(
                    queue_id,
                    candidate,
                    quality=candidate.quality,
                    progress_callback=lambda task: update_lx_fallback_progress(mode_job, task),
                    stop_requested=(lambda: not wait_for_task_ready(job)) if recording is not None else (lambda: stop_event.is_set() or task_should_stop(job)),
                )
                ensure_lx_queue_kept_quality(queue_task, candidate.quality)
                resolved_candidate, cleanup_candidate = resolve_queue_candidate(
                    track,
                    candidate,
                    queue_task,
                )
                if resolved_candidate is None:
                    raise LxFallbackError(
                        "LXServer substituted a source that does not provide "
                        f"the requested {candidate.quality} quality"
                    )
                accepted_candidate = resolved_candidate
                if recording is not None and not strict_match(recording, lx_recording_candidate(accepted_candidate)):
                    raise LxFallbackError("LX substituted a different recording")
                path = client.cached_path(
                    accepted_candidate,
                    quality=accepted_candidate.quality,
                )
                audio_info = verify_lx_audio(
                    path,
                    requested_quality=accepted_candidate.quality,
                )
                if recording is not None:
                    validate_recording_duration(recording, audio_info)
                path = client.organize_cached_path(
                    accepted_candidate,
                    quality=accepted_candidate.quality,
                )
            except LxFallbackStopped:
                raise
            except Exception as exc:
                message = redact_sensitive_text(exc)
                attempt_errors.append(
                    f"{candidate.source}/{candidate.quality}:"
                    f"{type(exc).__name__}:{message}"
                )
                cleanup_rejected_lx_output(client, cleanup_candidate, before)
                log(
                    f"job {job.label} rejected LX candidate {attempt}/{len(candidates)}: "
                    f"{message}"
                )
                continue

            if recording is None:
                sync_word_lyrics_for_paths([path], source_url=job.urls[0] if job.urls else "",
                    notification_context={"source": job.source, "chat_id": job.chat_id,
                        "message_id": job.message_id, "task_id": job.task_id}, logger=log)
            scan_triggered = trigger_navidrome_scan()
            set_download_progress_phase(
                mode_job,
                f"LX {lx_quality_label(candidate.quality)} 兜底完成",
                reset_transfer=True,
            )
            return LxFallbackOutcome(
                success=True,
                signature=signature,
                message=(
                    f"LX {lx_quality_label(accepted_candidate.quality)} "
                    "fallback completed"
                ),
                title=accepted_candidate.title,
                artist=accepted_candidate.artist,
                album=accepted_candidate.album,
                queue_id=queue_id,
                paths=(path,),
                created_paths=() if lx_path_existed_before(path, before) else (path,),
                scan_triggered=scan_triggered,
                quality=accepted_candidate.quality,
                sample_rate=int(audio_info["sample_rate"]),
                bit_depth=int(audio_info["bit_depth"]),
            )
        last_error = attempt_errors[-1] if attempt_errors else "no candidate completed"
        quality_text = "lossless/320k" if LX_FALLBACK_ALLOW_320 and recording is None else "lossless"
        raise LxFallbackError(
            f"no verified {quality_text} audio after {len(candidates)} strict candidate(s); "
            f"last={last_error}"
        )
    except LxFallbackStopped:
        raise
    except Exception as exc:
        message = redact_sensitive_text(exc)
        log(f"job {job.label} LX fallback failed: {message}")
        set_download_progress_phase(
            mode_job,
            "LX 高品质音源兜底失败",
            reset_transfer=True,
        )
        return LxFallbackOutcome(
            success=False,
            signature=signature,
            message=message,
        )


def selected_lx_candidate(job: Job) -> tuple[LxCandidate, str]:
    payload = job.payload if isinstance(job.payload, dict) else {}
    song_info = payload.get("song_info")
    if not isinstance(song_info, dict):
        raise LxFallbackError("LX selection payload is missing song metadata")
    source = str(payload.get("source") or "").strip().lower()
    songmid = str(payload.get("songmid") or "").strip()
    title = str(payload.get("title") or "").strip()
    artist = str(payload.get("artist") or "").strip()
    album = str(payload.get("album") or "").strip()
    raw_source = str(song_info.get("source") or source).strip().lower()
    raw_songmid = str(
        song_info.get("songmid") or song_info.get("songId") or ""
    ).strip()
    raw_title = str(song_info.get("name") or "").strip()
    raw_artist = str(song_info.get("singer") or "").strip()
    if (
        not source
        or source not in LX_FALLBACK_SEARCH_SOURCES
        or not songmid
        or not title
        or not artist
        or raw_source != source
        or raw_songmid != songmid
        or raw_title != title
        or raw_artist != artist
    ):
        raise LxFallbackError("LX selection payload failed the metadata check")
    quality = best_download_quality(
        song_info,
        allow_320=LX_FALLBACK_ALLOW_320,
    )
    if quality is None:
        raise LxFallbackError(
            "selected LX result has neither advertised lossless audio nor 320k MP3"
        )
    return (
        LxCandidate(
            song_info=dict(song_info),
            source=source,
            title=title,
            artist=artist,
            album=album,
            duration_seconds=parse_duration_seconds(song_info.get("interval")),
            score=0,
            quality=quality,
        ),
        songmid,
    )


def process_lx_music_job(job: Job) -> None:
    client: LxFallbackClient | None = None
    begin_download_progress(job, LX_FALLBACK_QUALITY)
    set_download_progress_context(
        job,
        LX_FALLBACK_QUALITY,
        1,
        phase="准备 LX 高品质音源下载",
    )
    try:
        if not LX_FALLBACK_ENABLED:
            raise LxFallbackError("LX Music integration is disabled")
        selected, _selected_songmid = selected_lx_candidate(job)
        client = LxFallbackClient(
            LX_FALLBACK_BASE_URL,
            output_root=LX_FALLBACK_OUTPUT_PATH,
            frontend_auth_file=Path(LX_FALLBACK_AUTH_FILE) if LX_FALLBACK_AUTH_FILE else None,
            server_music_root=LX_FALLBACK_SERVER_MUSIC_ROOT,
            request_timeout_seconds=LX_FALLBACK_REQUEST_TIMEOUT_SECONDS,
            queue_timeout_seconds=LX_FALLBACK_QUEUE_TIMEOUT_SECONDS,
            poll_seconds=LX_FALLBACK_POLL_SECONDS,
            logger=log,
        )
        track = AppleTrack(
            track_id="manual",
            title=selected.title,
            artist=selected.artist,
            album=selected.album,
            duration_seconds=selected.duration_seconds,
        )
        try:
            candidates = client.find_download_candidates(
                track,
                sources=LX_FALLBACK_SEARCH_SOURCES,
                allow_320=LX_FALLBACK_ALLOW_320,
            )[:LX_FALLBACK_MAX_CANDIDATES]
        except LxFallbackError as exc:
            log(
                f"job {job.label} cross-source quality refresh failed; "
                f"trying the selected source only: {exc}"
            )
            candidates = [selected]
        if not candidates:
            raise LxFallbackError("no strict cross-source high-quality candidates")

        def progress_callback(task: dict[str, Any]) -> None:
            update_lx_fallback_progress(job, task)
            sync_current_task_card(job)

        attempt_errors: list[str] = []
        completed: tuple[
            LxCandidate,
            str,
            Path,
            dict[str, int | float | str],
            dict[str, tuple[int, int, int, int]],
        ] | None = None
        for attempt, candidate in enumerate(candidates, start=1):
            if stop_event.is_set() or task_should_stop(job):
                raise LxFallbackStopped("LX high-quality download stopped")
            songmid = str(
                candidate.song_info.get("songmid")
                or candidate.song_info.get("songId")
                or ""
            )
            queue_id = lx_fallback_queue_id(
                job,
                "direct",
                candidate.source,
                songmid,
                attempt,
            )
            before = lx_output_snapshot()
            update_task_card(
                job.task_id,
                force=True,
                phase=(
                    f"高品质换源 {attempt}/{len(candidates)} · "
                    f"{lx_source_label(candidate.source)} "
                    f"{lx_quality_label(candidate.quality)}"
                ),
                title=f"{candidate.artist} - {candidate.title}",
                speed_bps=None,
                eta=None,
            )
            log(
                f"job {job.label} manual quality candidate "
                f"{attempt}/{len(candidates)}={candidate.source}/"
                f"{candidate.quality}:{candidate.artist} - {candidate.title}"
            )
            try:
                queue_task = client.enqueue_and_wait(
                    queue_id,
                    candidate,
                    quality=candidate.quality,
                    progress_callback=progress_callback,
                    stop_requested=lambda: stop_event.is_set() or task_should_stop(job),
                )
                ensure_lx_queue_kept_quality(queue_task, candidate.quality)
                path = client.cached_path(candidate, quality=candidate.quality)
                audio_info = verify_lx_audio(
                    path,
                    requested_quality=candidate.quality,
                )
                path = client.organize_cached_path(
                    candidate,
                    quality=candidate.quality,
                )
            except LxFallbackStopped:
                raise
            except Exception as exc:
                error_text = redact_sensitive_text(exc)
                attempt_errors.append(
                    f"{candidate.source}/{candidate.quality}: {error_text}"
                )
                cleanup_rejected_lx_output(client, candidate, before)
                log(
                    f"job {job.label} rejected manual quality candidate "
                    f"{attempt}/{len(candidates)}: {error_text}"
                )
                continue
            completed = (candidate, queue_id, path, audio_info, before)
            break

        if completed is None:
            last_error = attempt_errors[-1] if attempt_errors else "no candidate completed"
            quality_text = "lossless/320k" if LX_FALLBACK_ALLOW_320 else "lossless"
            raise LxFallbackError(
                f"all {len(candidates)} {quality_text} source/quality attempts failed; "
                f"last={last_error}"
            )

        candidate, queue_id, path, audio_info, before = completed
        stat = path.stat()
        existed_before = lx_path_existed_before(path, before)
        sync_word_lyrics_for_paths([path], source_url=job.urls[0] if job.urls else "",
            notification_context={"source": job.source, "chat_id": job.chat_id,
                "message_id": job.message_id, "task_id": job.task_id}, logger=log)
        scan_triggered = trigger_navidrome_scan()
        completion_paths = [str(path)]
        delete_paths = (
            []
            if existed_before
            else deletion_targets_for_media("lx_music", [path])
        )
        append_journal(
            {
                "event": "finished",
                "job": job_record(job),
                "time": time.time(),
                "failures": [],
                "lx_queue_id": queue_id,
                "lx_source": candidate.source,
                "lx_requested_quality": candidate.quality,
                "audio_info": audio_info,
                "attempt_failures": attempt_errors,
                "local_paths": completion_paths,
                "existing": existed_before,
                "scan_triggered": scan_triggered,
            }
        )
        complete_task_card(
            job.task_id,
            status="completed",
            phase=(
                f"LX {lx_quality_label(candidate.quality)} 已存在"
                if existed_before
                else "Navidrome 入库完成"
            )
            + f" · {lx_audio_quality_description(candidate.quality, audio_info)}",
            title=f"{candidate.artist} - {candidate.title}",
            finished_at=time.time(),
            percent=100.0,
            downloaded_bytes=stat.st_size,
            total_bytes=stat.st_size,
            total_estimated=0,
            speed_bps=None,
            eta="00:00",
            error="" if scan_triggered else "Navidrome 将由定时扫描发现",
            local_paths_json=json.dumps(completion_paths, ensure_ascii=False),
            delete_paths_json=json.dumps(delete_paths, ensure_ascii=False),
        )
    except LxFallbackStopped:
        append_journal(
            {
                "event": "finished",
                "job": job_record(job),
                "time": time.time(),
                "failures": [],
                "stopped": True,
            }
        )
        update_task_card(
            job.task_id,
            force=True,
            status="stopped",
            phase="已停止",
            finished_at=time.time(),
            speed_bps=None,
            eta=None,
            error="",
        )
    except Exception as exc:
        error_text = redact_sensitive_text(exc)
        append_journal(
            {
                "event": "finished",
                "job": job_record(job),
                "time": time.time(),
                "failures": [{"kind": "lx_music", "error": error_text}],
            }
        )
        update_task_card(
            job.task_id,
            force=True,
            status="failed",
            phase="LX 高品质音源下载失败",
            finished_at=time.time(),
            speed_bps=None,
            eta=None,
            error=error_text,
        )


def clean_query_text(text: str) -> str:
    text = re.sub(r"@\w+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" ：:，,。")


def search_query_from_text(text: str) -> str:
    stripped = text.strip()
    if not stripped or extract_urls(stripped):
        return ""
    first_token = stripped.split(maxsplit=1)[0]
    command = first_token.split("@", 1)[0].lower()
    remainder = stripped[len(first_token):].strip()
    if command in {"/alac", "/atmos", "/both", "alac", "atmos", "both", "/search", "search"}:
        return clean_query_text(remainder)
    if stripped.startswith("/"):
        return ""
    return clean_query_text(stripped)


def cleanup_search_cache() -> None:
    now = time.time()
    with search_cache_lock:
        expired = [key for key, (expiry, _mode, _result) in search_cache.items() if expiry <= now]
        for key in expired:
            search_cache.pop(key, None)
        expired_sessions = [key for key, (expiry, _session) in search_session_cache.items() if expiry <= now]
        for key in expired_sessions:
            search_session_cache.pop(key, None)
            lx_search_session_cache.pop(key, None)
        expired_lx_sessions = [
            key
            for key, (expiry, _session) in lx_search_session_cache.items()
            if expiry <= now
        ]
        for key in expired_lx_sessions:
            lx_search_session_cache.pop(key, None)
        for scope_key, session_key in list(active_search_session_by_chat.items()):
            if session_key in expired_sessions or session_key not in search_session_cache:
                active_search_session_by_chat.pop(scope_key, None)


def search_scope_key(chat_id: str, actor_id: str = "") -> str:
    actor_id = actor_id.strip()
    if actor_id:
        return f"{chat_id}:{actor_id}"
    return chat_id


def cache_search_result(chat_id: str, message_id: str, mode: str, result: SearchResult, index: int) -> str:
    cleanup_search_cache()
    seed = f"{time.time_ns()}:{chat_id}:{message_id}:{index}:{result.track_id}:{result.url}"
    key = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    with search_cache_lock:
        search_cache[key] = (time.time() + SEARCH_RESULT_TTL_SECONDS, mode, result)
    return key


def pop_search_result(key: str) -> tuple[str, SearchResult] | None:
    cleanup_search_cache()
    with search_cache_lock:
        cached = search_cache.pop(key, None)
    if not cached:
        return None
    expiry, mode, result = cached
    if expiry <= time.time():
        return None
    return mode, result


def cache_search_session(
    chat_id: str,
    message_id: str,
    query: str,
    mode: str,
    results: list[SearchResult],
    actor_id: str = "",
) -> str:
    cleanup_search_cache()
    scope_key = search_scope_key(chat_id, actor_id)
    seed = f"{time.time_ns()}:{chat_id}:{message_id}:{query}:{mode}:{len(results)}"
    key = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    with search_cache_lock:
        search_session_cache[key] = (
            time.time() + SEARCH_RESULT_TTL_SECONDS,
            SearchSession(
                chat_id=chat_id,
                message_id=message_id,
                scope_key=scope_key,
                query=query,
                mode=mode,
                results=list(results),
            ),
        )
        active_search_session_by_chat[scope_key] = key
    return key


def get_search_session(key: str) -> SearchSession | None:
    cleanup_search_cache()
    with search_cache_lock:
        cached = search_session_cache.get(key)
    if not cached:
        return None
    expiry, session = cached
    if expiry <= time.time():
        return None
    return session


def is_active_search_session(key: str, session: SearchSession) -> bool:
    cleanup_search_cache()
    with search_cache_lock:
        return active_search_session_by_chat.get(session.scope_key) == key


def invalidate_search_session(key: str) -> SearchSession | None:
    cleanup_search_cache()
    with search_cache_lock:
        cached = search_session_cache.pop(key, None)
        lx_search_session_cache.pop(key, None)
        if not cached:
            return None
        _expiry, session = cached
        if active_search_session_by_chat.get(session.scope_key) == key:
            active_search_session_by_chat.pop(session.scope_key, None)
        return session


def cache_lx_search_session(
    key: str,
    query: str,
    apple_page: int,
    results: list[LxSearchItem],
) -> LxSearchSession:
    session = LxSearchSession(
        query=query,
        apple_page=apple_page,
        results=list(results),
    )
    with search_cache_lock:
        lx_search_session_cache[key] = (
            time.time() + SEARCH_RESULT_TTL_SECONDS,
            session,
        )
    return session


def get_lx_search_session(key: str) -> LxSearchSession | None:
    cleanup_search_cache()
    with search_cache_lock:
        cached = lx_search_session_cache.get(key)
    if not cached:
        return None
    expiry, session = cached
    if expiry <= time.time():
        return None
    return session


def search_page_count(results: list[SearchResult]) -> int:
    return max(1, (len(results) + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)


def clamp_search_page(results: list[SearchResult], page: int) -> int:
    return min(max(page, 0), search_page_count(results) - 1)


def truncate_display_text(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def html_escape_text(value: str) -> str:
    return html.escape(value, quote=False)


def search_page_artwork_url(results: list[SearchResult], page: int) -> str:
    start = page * SEARCH_PAGE_SIZE
    end = min(start + SEARCH_PAGE_SIZE, len(results))
    for result in results[start:end]:
        if result.artwork_url:
            return result.artwork_url
    for result in results:
        if result.artwork_url:
            return result.artwork_url
    return ""


def build_search_results_page(
    query: str,
    mode: str,
    results: list[SearchResult],
    page: int,
    session_key: str,
) -> tuple[str, dict[str, Any], str]:
    page = clamp_search_page(results, page)
    page_count = search_page_count(results)
    start = page * SEARCH_PAGE_SIZE
    end = min(start + SEARCH_PAGE_SIZE, len(results))
    lines = [
        f"<b>{'统一音乐' if mode == 'alac' and any(item.unified_payload for item in results) else 'Apple Music'} 搜索：{html_escape_text(truncate_display_text(query, 32))}</b>",
        f"共找到 {len(results)} 首，第 {page + 1}/{page_count} 页。模式：<b>{html_escape_text(mode.upper())}</b>",
        "点下面编号选择下载。",
        "",
    ]
    keyboard: list[list[dict[str, str]]] = []
    number_row: list[dict[str, str]] = []
    for offset, result in enumerate(results[start:end], start + 1):
        title = html_escape_text(truncate_display_text(result.title, 22))
        meta_parts = [
            truncate_display_text(result.artist, 16),
            truncate_display_text(result.album, 18),
            result.release_year,
        ]
        if result.unified_payload:
            providers = list(dict.fromkeys(candidate["provider"] for candidate in result.unified_payload["candidates"]))
            meta_parts.append(" / ".join({"apple": "Apple", "lx": "LX", "sq": "SQ"}[provider] for provider in providers))
        if result.genre:
            meta_parts.append(truncate_display_text(result.genre, 8))
        meta = html_escape_text(" · ".join(item for item in meta_parts if item))
        lines.append(f"{offset}. <b>{title}</b>")
        if meta:
            lines.append(f"   {meta}")
        number_row.append(
            {
                "text": str(offset),
                "callback_data": f"{SEARCH_CALLBACK_PREFIX}:sel:{session_key}:{offset}",
            }
        )
        if len(number_row) == 4:
            keyboard.append(number_row)
            number_row = []
    if number_row:
        keyboard.append(number_row)
    if page_count > 1:
        nav_row: list[dict[str, str]] = []
        if page > 0:
            nav_row.append(
                {
                    "text": "上一页",
                    "callback_data": f"{SEARCH_CALLBACK_PREFIX}:page:{session_key}:{page - 1}",
                }
            )
        nav_row.append(
            {
                "text": f"{page + 1}/{page_count}",
                "callback_data": f"{SEARCH_CALLBACK_PREFIX}:noop:{session_key}",
            }
        )
        if page + 1 < page_count:
            nav_row.append(
                {
                    "text": "下一页",
                    "callback_data": f"{SEARCH_CALLBACK_PREFIX}:page:{session_key}:{page + 1}",
                }
            )
        keyboard.append(nav_row)
    if LX_FALLBACK_ENABLED and not any(item.unified_payload for item in results):
        keyboard.append(
            [
                {
                    "text": "转LX搜索",
                    "callback_data": (
                        f"{SEARCH_CALLBACK_PREFIX}:lx:{session_key}:{page}"
                    ),
                }
            ]
        )
    keyboard.append(
        [
            {
                "text": "取消本次搜索",
                "callback_data": f"{SEARCH_CALLBACK_PREFIX}:cancel:{session_key}",
            }
        ]
    )
    if any(item.unified_payload for item in results):
        lines.append("\n来源为搜索候选；点击后优先 Apple，再尝试 LX、SQ 无损。")
    text = "\n".join(lines)
    if len(text) > 1000:
        compact_lines = lines[:4]
        for offset, result in enumerate(results[start:end], start + 1):
            title = html_escape_text(truncate_display_text(result.title, 18))
            artist = html_escape_text(truncate_display_text(result.artist, 14))
            artist_suffix = f" - {artist}" if artist else ""
            source_suffix = ""
            if result.unified_payload:
                source_suffix = " · " + "/".join(dict.fromkeys({"apple": "Apple", "lx": "LX", "sq": "SQ"}[item["provider"]] for item in result.unified_payload["candidates"]))
            album_suffix = " · " + html_escape_text(truncate_display_text(result.album, 12)) if result.unified_payload and result.album else ""
            if result.unified_payload:
                version = version_fingerprint(RecordingCandidate.from_payload(result.unified_payload["candidates"][0]))
                version_names = {"live": "现场", "remaster": "重制", "acoustic": "不插电", "instrumental": "伴奏", "remix": "混音", "demo": "小样", "mono": "单声道", "edit": "剪辑", "cover": "翻唱"}
                if version:
                    album_suffix += " [" + "/".join(version_names[key] for key in sorted(version)) + "]"
            compact_lines.append(f"{offset}. <b>{title}</b>{artist_suffix}{album_suffix}{source_suffix}")
        text = "\n".join(compact_lines)
    return text, {"inline_keyboard": keyboard}, search_page_artwork_url(results, page)


def lx_search_page_count(results: list[LxSearchItem]) -> int:
    return max(1, (len(results) + LX_SEARCH_PAGE_SIZE - 1) // LX_SEARCH_PAGE_SIZE)


def clamp_lx_search_page(results: list[LxSearchItem], page: int) -> int:
    return min(max(page, 0), lx_search_page_count(results) - 1)


def lx_source_label(source: str) -> str:
    return {
        "kw": "酷我",
        "tx": "QQ",
        "wy": "网易云",
        "mg": "咪咕",
        "kg": "酷狗",
    }.get(source.casefold(), source.upper())


def lx_quality_label(quality: str) -> str:
    return {
        "master": "母带 FLAC",
        "hires": "Hi-Res FLAC",
        "flac24bit": "24bit FLAC",
        "flac": "FLAC",
        "320k": "320k MP3",
    }.get(quality.casefold(), quality.upper())


def lx_search_navigation_keyboard(
    session_key: str,
    *,
    has_apple_results: bool = True,
) -> list[list[dict[str, str]]]:
    keyboard: list[list[dict[str, str]]] = []
    if has_apple_results:
        keyboard.append(
            [
                {
                    "text": "返回 Apple 搜索",
                    "callback_data": f"{SEARCH_CALLBACK_PREFIX}:lxback:{session_key}",
                }
            ]
        )
    keyboard.append(
        [
            {
                "text": "取消本次搜索",
                "callback_data": f"{SEARCH_CALLBACK_PREFIX}:cancel:{session_key}",
            }
        ]
    )
    return keyboard


def build_lx_search_loading_page(
    query: str,
    session_key: str,
    *,
    has_apple_results: bool = True,
) -> tuple[str, dict[str, Any]]:
    lines = [
        f"<b>LX Music 高品质搜索：{html_escape_text(truncate_display_text(query, 32))}</b>",
    ]
    if not has_apple_results:
        lines.append("Apple Music 没有结果，已自动转到 LX Music。")
    lines.append("正在查询已导入的 LX 音乐源，请稍候……")
    return "\n".join(lines), {
        "inline_keyboard": lx_search_navigation_keyboard(
            session_key,
            has_apple_results=has_apple_results,
        )
    }


def build_lx_search_results_page(
    query: str,
    results: list[LxSearchItem],
    page: int,
    session_key: str,
    *,
    has_apple_results: bool = True,
) -> tuple[str, dict[str, Any]]:
    page = clamp_lx_search_page(results, page)
    page_count = lx_search_page_count(results)
    start = page * LX_SEARCH_PAGE_SIZE
    end = min(start + LX_SEARCH_PAGE_SIZE, len(results))
    lines = [
        f"<b>LX Music 高品质搜索：{html_escape_text(truncate_display_text(query, 32))}</b>",
    ]
    if not has_apple_results:
        lines.append("Apple Music 没有结果，已自动转到 LX Music。")
    if results:
        lossless_count = sum(is_lossless_quality(item.quality) for item in results)
        mp3_count = sum(item.quality.casefold() == MP3_320_QUALITY for item in results)
        lines.extend(
            [
                (
                    f"找到 {len(results)} 个候选（FLAC {lossless_count} / "
                    f"320k MP3 {mp3_count}），第 {page + 1}/{page_count} 页。"
                ),
                "无损优先；没有可用真无损时才用 320k MP3，MP3 不会标成 FLAC。",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "没有找到明确标注支持 FLAC 或 320k MP3 的候选。",
                (
                    "可以返回 Apple 搜索，或换一个更精确的关键词重试。"
                    if has_apple_results
                    else "可以换一个更精确的关键词重试。"
                ),
            ]
        )

    keyboard: list[list[dict[str, str]]] = []
    number_row: list[dict[str, str]] = []
    for offset, result in enumerate(results[start:end], start + 1):
        title = html_escape_text(truncate_display_text(result.title, 24))
        meta = " · ".join(
            item
            for item in (
                truncate_display_text(result.artist, 16),
                truncate_display_text(result.album, 16),
                result.interval,
                lx_quality_label(result.quality),
                lx_source_label(result.source),
            )
            if item
        )
        lines.append(f"{offset}. <b>{title}</b>")
        if meta:
            lines.append(f"   {html_escape_text(meta)}")
        number_row.append(
            {
                "text": str(offset),
                "callback_data": (
                    f"{SEARCH_CALLBACK_PREFIX}:lxsel:{session_key}:{offset}"
                ),
            }
        )
        if len(number_row) == 4:
            keyboard.append(number_row)
            number_row = []
    if number_row:
        keyboard.append(number_row)
    if page_count > 1:
        nav_row: list[dict[str, str]] = []
        if page > 0:
            nav_row.append(
                {
                    "text": "上一页",
                    "callback_data": (
                        f"{SEARCH_CALLBACK_PREFIX}:lxpage:{session_key}:{page - 1}"
                    ),
                }
            )
        nav_row.append(
            {
                "text": f"{page + 1}/{page_count}",
                "callback_data": f"{SEARCH_CALLBACK_PREFIX}:lxnoop:{session_key}",
            }
        )
        if page + 1 < page_count:
            nav_row.append(
                {
                    "text": "下一页",
                    "callback_data": (
                        f"{SEARCH_CALLBACK_PREFIX}:lxpage:{session_key}:{page + 1}"
                    ),
                }
            )
        keyboard.append(nav_row)
    keyboard.extend(
        lx_search_navigation_keyboard(
            session_key,
            has_apple_results=has_apple_results,
        )
    )
    return "\n".join(lines), {"inline_keyboard": keyboard}


def query_lx_flac_results(query: str) -> list[LxSearchItem]:
    client = LxFallbackClient(
        LX_FALLBACK_BASE_URL,
        output_root=LX_FALLBACK_OUTPUT_PATH,
        frontend_auth_file=Path(LX_FALLBACK_AUTH_FILE) if LX_FALLBACK_AUTH_FILE else None,
        server_music_root=LX_FALLBACK_SERVER_MUSIC_ROOT,
        request_timeout_seconds=LX_FALLBACK_REQUEST_TIMEOUT_SECONDS,
        queue_timeout_seconds=LX_FALLBACK_QUEUE_TIMEOUT_SECONDS,
        poll_seconds=LX_FALLBACK_POLL_SECONDS,
        logger=log,
    )
    return client.search_downloads(
        query,
        sources=LX_FALLBACK_SEARCH_SOURCES,
        quality=LX_FALLBACK_QUALITY,
        allow_320=LX_FALLBACK_ALLOW_320,
        max_results=LX_SEARCH_MAX_RESULTS,
    )


def send_search_results(
    chat_id: str,
    message_id: str,
    query: str,
    mode: str,
    results: list[SearchResult],
    actor_id: str = "",
) -> None:
    session_key = cache_search_session(chat_id, message_id, query, mode, results, actor_id)
    text, reply_markup, artwork_url = build_search_results_page(query, mode, results, 0, session_key)
    if artwork_url and send_photo(chat_id, artwork_url, text, reply_markup, "HTML"):
        return
    send_message(chat_id, text, reply_markup, "HTML")


def send_auto_lx_search_results(
    chat_id: str,
    message_id: str,
    query: str,
    mode: str,
    actor_id: str = "",
) -> None:
    session_key = cache_search_session(
        chat_id,
        message_id,
        query,
        mode,
        [],
        actor_id,
    )
    loading_text, loading_keyboard = build_lx_search_loading_page(
        query,
        session_key,
        has_apple_results=False,
    )
    loading_message = send_message(
        chat_id,
        loading_text,
        loading_keyboard,
        "HTML",
    )
    loading_message_id = str((loading_message or {}).get("message_id") or "")
    try:
        lx_results = query_lx_flac_results(query)
    except Exception as exc:
        error_text = redact_sensitive_text(exc)
        log(f"automatic LX search failed query={query!r}: {error_text}")
        invalidate_search_session(session_key)
        failure_text = "\n".join(
            [
                f"<b>LX Music 高品质搜索：{html_escape_text(truncate_display_text(query, 32))}</b>",
                "Apple Music 没有结果，LX 搜索也暂时不可用，请稍后重试。",
            ]
        )
        if loading_message_id:
            edit_message_text(
                chat_id,
                loading_message_id,
                failure_text,
                {"inline_keyboard": []},
                "HTML",
            )
        else:
            send_message(chat_id, failure_text, {"inline_keyboard": []}, "HTML")
        append_journal(
            {
                "event": "lx_search_auto_failed",
                "chat_id": chat_id,
                "query": query,
                "error": error_text,
                "time": time.time(),
            }
        )
        return

    lx_session = cache_lx_search_session(
        session_key,
        query,
        0,
        lx_results,
    )
    text, reply_markup = build_lx_search_results_page(
        lx_session.query,
        lx_session.results,
        0,
        session_key,
        has_apple_results=False,
    )
    edited = bool(
        loading_message_id
        and edit_message_text(
            chat_id,
            loading_message_id,
            text,
            reply_markup,
            "HTML",
        )
    )
    if not edited:
        send_message(chat_id, text, reply_markup, "HTML")
    append_journal(
        {
            "event": "lx_search_auto_opened",
            "chat_id": chat_id,
            "query": query,
            "result_count": len(lx_session.results),
            "time": time.time(),
        }
    )


def close_search_message(
    chat_id: str,
    message: dict[str, Any],
    text: str,
) -> None:
    current_message_id = str(message.get("message_id") or "")
    if not current_message_id:
        return
    empty_keyboard = {"inline_keyboard": []}
    if message.get("photo"):
        edit_message_caption(chat_id, current_message_id, text, empty_keyboard, "HTML")
    else:
        edit_message_text(chat_id, current_message_id, text, empty_keyboard, "HTML")


def edit_search_message(
    chat_id: str,
    message: dict[str, Any],
    text: str,
    reply_markup: dict[str, Any],
    *,
    artwork_url: str = "",
) -> bool:
    current_message_id = str(message.get("message_id") or "")
    if not current_message_id:
        return False
    if message.get("photo"):
        if artwork_url and edit_message_photo(
            chat_id,
            current_message_id,
            artwork_url,
            text,
            reply_markup,
            "HTML",
        ):
            return True
        return edit_message_caption(
            chat_id,
            current_message_id,
            text,
            reply_markup,
            "HTML",
        )
    return edit_message_text(
        chat_id,
        current_message_id,
        text,
        reply_markup,
        "HTML",
    )


def search_session_stale_text(session: SearchSession) -> str:
    return (
        "这条 Apple Music 搜索结果已失效。\n"
        f"关键词：<b>{html_escape_text(truncate_display_text(session.query, 40))}</b>\n"
        "原因：你已经发起了新的搜索，或这条搜索已被取消。"
    )


def queue_download(
    source: str,
    chat_id: str,
    message_id: str,
    urls: list[str],
    mode: str,
    kind: str = "apple_music",
    title: str = "",
    payload: dict[str, Any] | None = None,
) -> str | None:
    if (kind == "apple_music" and payload is None
            and unified_eligible(UNIFIED_MUSIC_ENABLED, mode, urls)):
        payload = {"unified_music": {"schema": 1, "apple_url": urls[0]}}
    received_at = time.time()
    raw_job = {
        "source": source,
        "chat_id": chat_id,
        "message_id": message_id,
        "urls": urls,
        "mode": mode,
        "kind": kind,
        "received_at": received_at,
        "payload": payload,
    }
    job_key = f"{source}:{chat_id}/{message_id}"
    existing_task = task_card_store.get_by_job_key(job_key)
    if existing_task is not None:
        task_id = int(existing_task["id"])
        if source == "telegram":
            update_task_card_message(task_id, force=True)
            return None
        return f"任务已存在：#{task_id:06d}\n{queue_status()}"
    task_id = task_card_store.create(
        job_key=job_key,
        job=raw_job,
        source=source,
        chat_id=chat_id,
        kind=kind,
        mode=mode,
        title=title,
        created_at=received_at,
    )
    job = Job(
        source=source,
        chat_id=chat_id,
        message_id=message_id,
        urls=urls,
        mode=mode,
        kind=kind,
        received_at=received_at,
        task_id=task_id,
        payload=payload,
    )
    append_journal({"event": "queued", "job": job_record(job), "time": time.time()})
    job_queue.put(job)
    if kind == "youtube_audio":
        description = "YouTube 音乐"
    elif kind == "youtube_video":
        description = "YouTube 视频"
    elif kind == "bilibili_audio":
        description = "哔哩哔哩音乐"
    elif kind == "bilibili_video":
        description = "哔哩哔哩视频"
    elif kind == "lx_music":
        description = "LX Music 高品质音源"
    else:
        description = f"Apple Music（{mode}）"
    if source == "telegram":
        update_task_card_message(task_id, force=True)
        return None
    return f"已入队：{description}，{len(urls)} 个链接。\n{queue_status()}"


def youtube_command_kind(text: str) -> str:
    first_token = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
    command = first_token.split("@", 1)[0]
    if command in {"/ytmusic", "/ytaudio", "ytmusic", "ytaudio"}:
        return "youtube_audio"
    if command in {"/ytvideo", "ytvideo"}:
        return "youtube_video"
    return ""


def bilibili_command_kind(text: str) -> str:
    first_token = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
    command = first_token.split("@", 1)[0]
    if command in {"/biliaudio", "/bilimusic", "biliaudio", "bilimusic"}:
        return "bilibili_audio"
    if command in {"/bilivideo", "bilivideo"}:
        return "bilibili_video"
    return ""


def cleanup_youtube_choices() -> None:
    now = time.time()
    with youtube_choice_lock:
        for key, (expiry, _choice) in list(youtube_choice_cache.items()):
            if expiry <= now:
                youtube_choice_cache.pop(key, None)


def quick_youtube_metadata(url: str) -> tuple[str, str]:
    if not YOUTUBE_CHOICE_METADATA_ENABLED:
        return "", ""
    endpoint = "https://www.youtube.com/oembed?" + urllib.parse.urlencode(
        {
            "url": url,
            "format": "json",
        }
    )
    request = urllib.request.Request(
        endpoint,
        headers={"user-agent": "AppleMusicIngest/1.0"},
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=YOUTUBE_CHOICE_METADATA_TIMEOUT_SECONDS,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        log(
            "YouTube quick metadata unavailable; showing choice immediately "
            f"without title: {type(exc).__name__}"
        )
        return "", ""
    return (
        str(payload.get("title") or "").strip(),
        str(payload.get("author_name") or "").strip(),
    )


def cache_youtube_choice(
    chat_id: str,
    actor_id: str,
    url: str,
) -> tuple[str, YouTubeChoice]:
    cleanup_youtube_choices()
    started_at = time.monotonic()
    clean_url = validate_youtube_url(url)
    if is_youtube_collection_hint(clean_url):
        probe = probe_youtube_url(clean_url)
        choice_url = probe.url
        title = probe.title
        uploader = probe.uploader
        item_count = probe.item_count
        is_collection = probe.is_collection
        truncated = probe.truncated
        preflight = "full-collection"
    else:
        title, uploader = quick_youtube_metadata(clean_url)
        choice_url = clean_url
        title = title or "YouTube 单个视频（标题将在下载时读取）"
        item_count = 1
        is_collection = False
        truncated = False
        preflight = "oembed" if uploader or "标题将在下载时读取" not in title else "deferred"
    seed = f"{time.time_ns()}:{chat_id}:{actor_id}:{choice_url}"
    key = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    choice = YouTubeChoice(
        chat_id=chat_id,
        actor_id=actor_id,
        url=choice_url,
        title=title,
        uploader=uploader,
        item_count=item_count,
        is_collection=is_collection,
        truncated=truncated,
    )
    with youtube_choice_lock:
        youtube_choice_cache[key] = (time.time() + YOUTUBE_CHOICE_TTL_SECONDS, choice)
    log(
        "YouTube choice ready "
        f"elapsed={time.monotonic() - started_at:.2f}s "
        f"preflight={preflight} collection={is_collection}"
    )
    return key, choice


def pop_youtube_choice(key: str) -> YouTubeChoice | None:
    cleanup_youtube_choices()
    with youtube_choice_lock:
        cached = youtube_choice_cache.pop(key, None)
    if not cached:
        return None
    expiry, choice = cached
    return choice if expiry > time.time() else None


def get_youtube_choice(key: str) -> YouTubeChoice | None:
    cleanup_youtube_choices()
    with youtube_choice_lock:
        cached = youtube_choice_cache.get(key)
    if not cached:
        return None
    expiry, choice = cached
    return choice if expiry > time.time() else None


def send_youtube_prompt(chat_id: str, actor_id: str, url: str) -> None:
    key, choice = cache_youtube_choice(chat_id, actor_id, url)
    item_text = (
        f"至少 {choice.item_count} 项"
        if choice.truncated
        else (f"{choice.item_count} 项" if choice.is_collection else "单个内容")
    )
    lines = [
        "检测到 YouTube 链接，请选择入库方式：",
        f"标题：{truncate_display_text(choice.title, 80)}",
    ]
    if choice.uploader:
        lines.append(f"频道：{truncate_display_text(choice.uploader, 80)}")
    lines.append(f"范围：{item_text}")
    if choice.truncated or choice.item_count > YOUTUBE_PLAYLIST_MAX_ITEMS:
        lines.append(f"超过安全上限 {YOUTUBE_PLAYLIST_MAX_ITEMS} 项，本次不允许加入队列。")
        reply_markup = {
            "inline_keyboard": [[{"text": "取消", "callback_data": f"{YOUTUBE_CALLBACK_PREFIX}:cancel:{key}"}]]
        }
    else:
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "音乐入库 Navidrome", "callback_data": f"{YOUTUBE_CALLBACK_PREFIX}:audio:{key}"},
                    {"text": "视频入库 Emby", "callback_data": f"{YOUTUBE_CALLBACK_PREFIX}:video:{key}"},
                ],
                [{"text": "取消", "callback_data": f"{YOUTUBE_CALLBACK_PREFIX}:cancel:{key}"}],
            ]
        }
    send_message(chat_id, "\n".join(lines), reply_markup)


def cleanup_bilibili_choices() -> None:
    now = time.time()
    with bilibili_choice_lock:
        for key, (expiry, _choice) in list(bilibili_choice_cache.items()):
            if expiry <= now:
                bilibili_choice_cache.pop(key, None)


def cache_bilibili_choice(
    chat_id: str,
    actor_id: str,
    url: str,
) -> tuple[str, BilibiliChoice]:
    cleanup_bilibili_choices()
    probe = probe_bilibili_url(url)
    seed = f"{time.time_ns()}:{chat_id}:{actor_id}:{probe.url}"
    key = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    choice = BilibiliChoice(
        chat_id=chat_id,
        actor_id=actor_id,
        url=probe.url,
        title=probe.title,
        uploader=probe.uploader,
        item_count=probe.item_count,
        is_collection=probe.is_collection,
        truncated=probe.truncated,
    )
    with bilibili_choice_lock:
        bilibili_choice_cache[key] = (
            time.time() + BILIBILI_CHOICE_TTL_SECONDS,
            choice,
        )
    return key, choice


def pop_bilibili_choice(key: str) -> BilibiliChoice | None:
    cleanup_bilibili_choices()
    with bilibili_choice_lock:
        cached = bilibili_choice_cache.pop(key, None)
    if not cached:
        return None
    expiry, choice = cached
    return choice if expiry > time.time() else None


def get_bilibili_choice(key: str) -> BilibiliChoice | None:
    cleanup_bilibili_choices()
    with bilibili_choice_lock:
        cached = bilibili_choice_cache.get(key)
    if not cached:
        return None
    expiry, choice = cached
    return choice if expiry > time.time() else None


def send_bilibili_prompt(chat_id: str, actor_id: str, url: str) -> None:
    key, choice = cache_bilibili_choice(chat_id, actor_id, url)
    item_text = (
        f"至少 {choice.item_count} 项"
        if choice.truncated
        else (f"{choice.item_count} 项" if choice.is_collection else "单个内容")
    )
    lines = [
        "检测到哔哩哔哩链接，请选择入库方式：",
        f"标题：{truncate_display_text(choice.title, 80)}",
    ]
    if choice.uploader:
        lines.append(f"UP 主：{truncate_display_text(choice.uploader, 80)}")
    lines.append(f"范围：{item_text}")
    if choice.truncated or choice.item_count > BILIBILI_PLAYLIST_MAX_ITEMS:
        lines.append(f"超过安全上限 {BILIBILI_PLAYLIST_MAX_ITEMS} 项，本次不允许加入队列。")
        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "取消",
                        "callback_data": f"{BILIBILI_CALLBACK_PREFIX}:cancel:{key}",
                    }
                ]
            ]
        }
    else:
        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "音乐入库 Navidrome",
                        "callback_data": f"{BILIBILI_CALLBACK_PREFIX}:audio:{key}",
                    },
                    {
                        "text": "视频入库 Emby",
                        "callback_data": f"{BILIBILI_CALLBACK_PREFIX}:video:{key}",
                    },
                ],
                [
                    {
                        "text": "取消",
                        "callback_data": f"{BILIBILI_CALLBACK_PREFIX}:cancel:{key}",
                    }
                ],
            ]
        }
    send_message(chat_id, "\n".join(lines), reply_markup)


def display_youtube_path(path: str, kind: str) -> str:
    if kind == "youtube_audio" and path.startswith("/youtube-music"):
        return YOUTUBE_AUDIO_DISPLAY_ROOT.rstrip("/") + path[len("/youtube-music") :]
    if kind == "youtube_video" and path.startswith("/youtube-video"):
        return YOUTUBE_VIDEO_DISPLAY_ROOT.rstrip("/") + path[len("/youtube-video") :]
    return path


def display_bilibili_path(path: str, kind: str) -> str:
    if kind == "bilibili_audio" and path.startswith("/bilibili-music"):
        return BILIBILI_AUDIO_DISPLAY_ROOT.rstrip("/") + path[len("/bilibili-music") :]
    if kind == "bilibili_video" and path.startswith("/bilibili-video"):
        return BILIBILI_VIDEO_DISPLAY_ROOT.rstrip("/") + path[len("/bilibili-video") :]
    return path


def media_path_size(path: str) -> int | None:
    target = Path(path)
    try:
        if target.is_file():
            return target.stat().st_size
        if target.is_dir():
            return sum(item.stat().st_size for item in target.rglob("*") if item.is_file())
    except OSError:
        return None
    return None


def format_file_size(size: int | None) -> str:
    if size is None:
        return "大小未知"
    value = float(max(0, size))
    units = ("B", "KB", "MB", "GB", "TB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if value < 1024 or candidate == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} B"
    return f"{value:.2f} {unit}"


def sized_paths(paths: list[str]) -> tuple[list[tuple[str, int | None]], int | None]:
    entries = [(path, media_path_size(path)) for path in paths]
    sizes = [size for _path, size in entries if size is not None]
    total = sum(sizes) if len(sizes) == len(entries) else None
    return entries, total


def video_platform_config(
    platform: str,
) -> tuple[str, str, Path, Path, Path, str, str]:
    if platform == "youtube":
        return (
            "YouTube",
            "youtube_video",
            YOUTUBE_DB_PATH,
            YOUTUBE_VIDEO_ROOT,
            YOUTUBE_STATE_DIR,
            "video_id",
            "youtube",
        )
    if platform == "bilibili":
        return (
            "哔哩哔哩",
            "bilibili_video",
            BILIBILI_DB_PATH,
            BILIBILI_VIDEO_ROOT,
            BILIBILI_STATE_DIR,
            "media_id",
            "bilibili",
        )
    raise VideoDeleteError(f"不支持的视频平台：{platform}")


def video_platform_marker(platform: str, media_id: str) -> str:
    if platform == "youtube":
        return f"[YT_{media_id}]"
    if platform == "bilibili":
        return f"[BILI_{media_id}]"
    raise VideoDeleteError(f"不支持的视频平台：{platform}")


def list_downloaded_videos() -> list[VideoDeleteEntry]:
    entries: list[VideoDeleteEntry] = []
    for platform in ("youtube", "bilibili"):
        _label, kind, database_path, _root, _state, id_column, _archive_prefix = (
            video_platform_config(platform)
        )
        if not database_path.exists():
            continue
        try:
            with sqlite3.connect(database_path, timeout=10) as database:
                rows = database.execute(
                    f"""
                    SELECT {id_column}, source_url, title, output_path, downloaded_at
                    FROM downloads
                    WHERE kind = ?
                    ORDER BY downloaded_at DESC
                    """,
                    (kind,),
                ).fetchall()
        except sqlite3.Error as exc:
            log(f"failed to list {platform} video downloads: {exc}")
            continue
        for media_id, source_url, title, output_path, downloaded_at in rows:
            entries.append(
                VideoDeleteEntry(
                    platform=platform,
                    media_id=str(media_id),
                    source_url=str(source_url),
                    title=str(title),
                    output_path=str(output_path),
                    downloaded_at=float(downloaded_at),
                )
            )
    entries.sort(key=lambda item: item.downloaded_at, reverse=True)
    return entries


def create_video_delete_session(
    chat_id: str,
    actor_id: str,
) -> tuple[str, VideoDeleteSession] | None:
    entries = tuple(list_downloaded_videos())
    if not entries:
        return None
    session = VideoDeleteSession(
        chat_id=chat_id,
        actor_id=actor_id,
        entries=entries,
    )
    now = time.monotonic()
    key = secrets.token_hex(5)
    with video_delete_cache_lock:
        for cached_key, (created_at, cached_session) in list(
            video_delete_session_cache.items()
        ):
            if (
                now - created_at > VIDEO_DELETE_TTL_SECONDS
                or (
                    cached_session.chat_id == chat_id
                    and cached_session.actor_id == actor_id
                )
            ):
                video_delete_session_cache.pop(cached_key, None)
        video_delete_session_cache[key] = (now, session)
    return key, session


def get_video_delete_session(key: str) -> VideoDeleteSession | None:
    now = time.monotonic()
    with video_delete_cache_lock:
        cached = video_delete_session_cache.get(key)
        if not cached:
            return None
        created_at, session = cached
        if now - created_at > VIDEO_DELETE_TTL_SECONDS:
            video_delete_session_cache.pop(key, None)
            return None
        return session


def invalidate_video_delete_session(key: str) -> VideoDeleteSession | None:
    with video_delete_cache_lock:
        cached = video_delete_session_cache.pop(key, None)
    return cached[1] if cached else None


def video_delete_page_count(entries: tuple[VideoDeleteEntry, ...]) -> int:
    return max(1, (len(entries) + VIDEO_DELETE_PAGE_SIZE - 1) // VIDEO_DELETE_PAGE_SIZE)


def build_video_delete_page(
    session: VideoDeleteSession,
    key: str,
    page: int,
) -> tuple[str, dict[str, Any], int]:
    page_count = video_delete_page_count(session.entries)
    page = min(max(0, page), page_count - 1)
    start = page * VIDEO_DELETE_PAGE_SIZE
    visible = session.entries[start : start + VIDEO_DELETE_PAGE_SIZE]
    lines = [
        "删除在线视频",
        "这里只列出本机器人下载的 YouTube / 哔哩哔哩视频。",
        f"第 {page + 1}/{page_count} 页，共 {len(session.entries)} 项；请选择要删除的视频：",
    ]
    keyboard: list[list[dict[str, str]]] = []
    for offset, entry in enumerate(visible):
        index = start + offset
        platform_text = "YT" if entry.platform == "youtube" else "B站"
        keyboard.append(
            [
                {
                    "text": f"{platform_text} · {truncate_display_text(entry.title, 44)}",
                    "callback_data": (
                        f"{VIDEO_DELETE_CALLBACK_PREFIX}:pick:{key}:{index}"
                    ),
                }
            ]
        )
    navigation: list[dict[str, str]] = []
    if page > 0:
        navigation.append(
            {
                "text": "上一页",
                "callback_data": f"{VIDEO_DELETE_CALLBACK_PREFIX}:page:{key}:{page - 1}",
            }
        )
    navigation.append(
        {
            "text": f"{page + 1}/{page_count}",
            "callback_data": f"{VIDEO_DELETE_CALLBACK_PREFIX}:noop:{key}:{page}",
        }
    )
    if page + 1 < page_count:
        navigation.append(
            {
                "text": "下一页",
                "callback_data": f"{VIDEO_DELETE_CALLBACK_PREFIX}:page:{key}:{page + 1}",
            }
        )
    keyboard.append(navigation)
    keyboard.append(
        [
            {
                "text": "关闭",
                "callback_data": f"{VIDEO_DELETE_CALLBACK_PREFIX}:cancel:{key}",
            }
        ]
    )
    return "\n".join(lines), {"inline_keyboard": keyboard}, page


def send_video_delete_menu(chat_id: str, actor_id: str) -> bool:
    cached = create_video_delete_session(chat_id, actor_id)
    if not cached:
        return False
    key, session = cached
    text, reply_markup, _page = build_video_delete_page(session, key, 0)
    send_message(chat_id, text, reply_markup)
    return True


def load_video_delete_entry(entry: VideoDeleteEntry) -> VideoDeleteEntry | None:
    _label, kind, database_path, _root, _state, id_column, _archive_prefix = (
        video_platform_config(entry.platform)
    )
    if not database_path.exists():
        return None
    with sqlite3.connect(database_path, timeout=10) as database:
        row = database.execute(
            f"""
            SELECT source_url, title, output_path, downloaded_at
            FROM downloads
            WHERE kind = ? AND {id_column} = ?
            """,
            (kind, entry.media_id),
        ).fetchone()
    if not row:
        return None
    return VideoDeleteEntry(
        platform=entry.platform,
        media_id=entry.media_id,
        source_url=str(row[0]),
        title=str(row[1]),
        output_path=str(row[2]),
        downloaded_at=float(row[3]),
    )


def video_delete_directory(entry: VideoDeleteEntry) -> Path:
    _label, _kind, _database_path, root_path, _state, _id_column, _archive_prefix = (
        video_platform_config(entry.platform)
    )
    root = root_path.resolve()
    output = Path(entry.output_path).resolve(strict=False)
    try:
        output.relative_to(root)
    except ValueError as exc:
        raise VideoDeleteError("记录路径不在受管的视频目录内，已拒绝删除。") from exc
    directory = output.parent
    if directory == root:
        raise VideoDeleteError("记录指向视频库根目录，已拒绝删除。")
    marker = video_platform_marker(entry.platform, entry.media_id)
    if marker not in directory.name:
        raise VideoDeleteError("视频目录缺少平台 ID 标记，已拒绝删除。")
    return directory


def video_delete_display_path(entry: VideoDeleteEntry, path: Path) -> str:
    if entry.platform == "youtube":
        return display_youtube_path(str(path), "youtube_video")
    return display_bilibili_path(str(path), "bilibili_video")


def remove_video_archive_entry(entry: VideoDeleteEntry) -> None:
    _label, _kind, _database_path, _root, state_dir, _id_column, archive_prefix = (
        video_platform_config(entry.platform)
    )
    archive_path = state_dir / "archive-video.txt"
    if not archive_path.exists():
        return
    target = f"{archive_prefix} {entry.media_id}"
    lines = archive_path.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = [line for line in lines if line.strip() != target]
    if len(kept) == len(lines):
        return
    temporary = archive_path.with_name(
        f".{archive_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        text = "\n".join(kept)
        if text:
            text += "\n"
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, archive_path)
    finally:
        temporary.unlink(missing_ok=True)


def delete_video_entry(
    selected: VideoDeleteEntry,
) -> tuple[VideoDeleteEntry, Path, int | None, bool, bool]:
    entry = load_video_delete_entry(selected)
    if not entry:
        raise VideoDeleteError("这条下载记录已经不存在，请重新打开删除列表。")
    label, kind, database_path, _root, _state, id_column, _archive_prefix = (
        video_platform_config(entry.platform)
    )
    directory = video_delete_directory(entry)
    size = media_path_size(str(directory))
    existed = directory.exists()
    if existed:
        shutil.rmtree(directory)
    remove_video_archive_entry(entry)
    with sqlite3.connect(database_path, timeout=10) as database:
        cursor = database.execute(
            f"DELETE FROM downloads WHERE kind = ? AND {id_column} = ?",
            (kind, entry.media_id),
        )
        database.commit()
    if cursor.rowcount != 1:
        raise VideoDeleteError(f"{label} 下载记录没有被删除，请检查状态库。")
    scan_triggered = trigger_emby_scan()
    return entry, directory, size, existed, scan_triggered


def storefront_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in {"music.apple.com", "beta.music.apple.com", "geo.music.apple.com"}:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return ""
    storefront = parts[0].lower()
    if STORE_FRONT_RE.fullmatch(storefront):
        return storefront
    return ""


def parse_mode(text: str) -> str:
    first_token = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
    command = first_token.split("@", 1)[0]
    if command in {"/alac", "alac"}:
        return "alac"
    if command in {"/atmos", "atmos"}:
        return "atmos"
    if command in {"/both", "both"}:
        return "both"
    if DEFAULT_MODE in VALID_MODES:
        return DEFAULT_MODE
    return "alac"


def chat_allowed(chat_id: str) -> bool:
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


def text_with_entity_urls(message: dict[str, Any]) -> str:
    text = message.get("text") or message.get("caption") or ""
    extra_urls: list[str] = []
    for entity_name in ("entities", "caption_entities"):
        entities = message.get(entity_name) or []
        if not isinstance(entities, list):
            continue
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            url = ""
            if entity.get("type") == "text_link":
                url = str(entity.get("url") or "").strip()
            if url and url not in text and url not in extra_urls:
                extra_urls.append(url)
    if extra_urls:
        return "\n".join([text, *extra_urls]) if text else "\n".join(extra_urls)
    return text


def get_message_text(update: dict[str, Any]) -> tuple[str, str, int, str] | None:
    message = telegram_message_from_update(update)
    if not message:
        return None
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    message_id = int(message.get("message_id", 0))
    sender = message.get("from") or {}
    # Telegram hides the real user behind sender_chat for anonymous admins.
    # Leaving actor_id empty lets the first member of the allowed chat choose.
    sender_id = "" if message.get("sender_chat") else str(sender.get("id") or "")
    text = text_with_entity_urls(message)
    return chat_id, text, message_id, sender_id


def telegram_message_from_update(update: dict[str, Any]) -> dict[str, Any] | None:
    message = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
        or update.get("business_message")
        or update.get("edited_business_message")
    )
    if not isinstance(message, dict):
        return None
    return message


def synology_user_allowed(user_id: str) -> bool:
    return not SYN_CHAT_ALLOWED_USER_IDS or user_id in SYN_CHAT_ALLOWED_USER_IDS


def first_text(data: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = data.get(name)
        if value is None:
            continue
        if isinstance(value, list):
            value = value[0] if value else ""
        value_text = str(value).strip()
        if value_text:
            return value_text
    return ""


def parse_synology_body(content_type: str, raw_body: bytes) -> dict[str, Any]:
    text = raw_body.decode("utf-8", errors="replace")
    data: dict[str, Any] = {}
    if "application/json" in content_type:
        try:
            parsed = json.loads(text or "{}")
            if isinstance(parsed, dict):
                data.update(parsed)
        except json.JSONDecodeError:
            pass
    else:
        parsed_qs = urllib.parse.parse_qs(text, keep_blank_values=True)
        data.update({key: values[0] if values else "" for key, values in parsed_qs.items()})

    payload = data.get("payload")
    if isinstance(payload, str) and payload.strip():
        try:
            parsed_payload = json.loads(payload)
            if isinstance(parsed_payload, dict):
                data.update({f"payload.{key}": value for key, value in parsed_payload.items()})
                for key, value in parsed_payload.items():
                    data.setdefault(key, value)
        except json.JSONDecodeError:
            pass
    return data


def tcp_ready(endpoint: str, timeout: float = 2.0) -> bool:
    host, port_text = endpoint.rsplit(":", 1)
    host = host.strip("[]")
    try:
        with socket.create_connection((host, int(port_text)), timeout=timeout):
            return True
    except OSError:
        return False


def endpoint_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{host}:{port}"


def endpoint_to_host_port(endpoint: str) -> tuple[str, int]:
    host, port_text = endpoint.rsplit(":", 1)
    return host.strip("[]"), int(port_text)


def pipe_sockets(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    try:
        while not stop_event.is_set():
            readable, _, _ = select.select(sockets, [], [], 2)
            for sock in readable:
                data = sock.recv(65536)
                if not data:
                    return
                target = right if sock is left else left
                target.sendall(data)
    except OSError:
        return
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass


def forward_loop(name: str, local_endpoint: str, target_endpoint: str) -> None:
    local_host, local_port = endpoint_to_host_port(local_endpoint)
    target_host, target_port = endpoint_to_host_port(target_endpoint)
    if local_host in {"localhost", "::1"}:
        local_host = "127.0.0.1"

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((local_host, local_port))
        server.listen(64)
        server.settimeout(2)
    except OSError as exc:
        log(f"wrapper forward {name} failed to bind {local_host}:{local_port}: {exc}")
        return

    log(f"wrapper forward {name}: {local_host}:{local_port} -> {target_host}:{target_port}")
    try:
        while not stop_event.is_set():
            try:
                client, _addr = server.accept()
            except socket.timeout:
                continue
            try:
                upstream = socket.create_connection((target_host, target_port), timeout=10)
            except OSError as exc:
                log(f"wrapper forward {name} target connect failed: {exc}")
                client.close()
                continue
            threading.Thread(target=pipe_sockets, args=(client, upstream), daemon=True).start()
    finally:
        server.close()


def start_wrapper_forwarders() -> None:
    if not WRAPPER_FORWARD_ENABLED:
        return
    forwards = [
        ("account", endpoint_from_url(WRAPPER_ACCOUNT_URL), WRAPPER_FORWARD_ACCOUNT_TARGET),
        ("decrypt", WRAPPER_DECRYPT_IP, WRAPPER_FORWARD_DECRYPT_TARGET),
        ("m3u8", WRAPPER_M3U8_IP, WRAPPER_FORWARD_M3U8_TARGET),
    ]
    for name, local_endpoint, target_endpoint in forwards:
        threading.Thread(
            target=forward_loop,
            args=(name, local_endpoint, target_endpoint),
            daemon=True,
        ).start()


def wait_for_wrapper() -> None:
    while not stop_event.is_set():
        if wait_for_wrapper_ports(timeout_seconds=10, log_wait=False):
            return
        log("waiting for wrapper-v2 HTTP and decrypt endpoints")
        time.sleep(10)


def wrapper_http_healthy() -> bool:
    health_url = WRAPPER_URL.rstrip("/") + "/health"
    request = urllib.request.Request(
        health_url,
        headers={"accept": "application/json", "connection": "close"},
    )
    try:
        with DIRECT_OPENER.open(request, timeout=8) as response:
            payload = json.loads(read_response_text(response))
        return response.status == 200 and payload.get("status") == "ok"
    except Exception:
        return False


def wait_for_wrapper_ports(timeout_seconds: float, log_wait: bool = True) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while not stop_event.is_set() and time.monotonic() <= deadline:
        if tcp_ready(WRAPPER_DECRYPT_IP) and wrapper_http_healthy():
            return True
        if log_wait:
            log("waiting for wrapper health after recovery")
        time.sleep(5)
    return False


def wait_for_wrapper_manager() -> None:
    while not stop_event.is_set():
        if tcp_ready(WRAPPER_MANAGER_ENDPOINT):
            return
        log(f"waiting for wrapper-manager at {WRAPPER_MANAGER_ENDPOINT}")
        time.sleep(10)


def wait_for_backend() -> None:
    if BACKEND == "applemusicdecrypt":
        wait_for_wrapper_manager()
    else:
        wait_for_wrapper()


def output_for_mode(mode: str) -> Path:
    if mode == "atmos":
        return ATMOS_OUTPUT_PATH
    return ALAC_OUTPUT_PATH


def begin_download_progress(job: Job, mode: str) -> None:
    global current_download_progress
    now = time.time()
    with download_progress_lock:
        current_download_progress = DownloadProgress(
            job_label=job.label,
            mode=mode,
            phase="准备下载",
            started_at=now,
            updated_at=now,
            url_index=0,
            url_total=len(job.urls),
        )


def set_download_progress_context(
    job: Job,
    mode: str,
    url_index: int,
    phase: str = "读取媒体信息",
) -> None:
    global current_download_progress
    with download_progress_lock:
        progress = current_download_progress
        if progress is None or progress.job_label != job.label:
            return
        current_download_progress = replace(
            progress,
            mode=mode,
            phase=phase,
            updated_at=time.time(),
            url_index=url_index,
            url_total=len(job.urls),
            track_index=0,
            track_total=0,
            title="",
            percent=None,
            total_bytes=None,
            total_estimated=False,
            speed_bytes_per_second=None,
            eta="",
            fragment=None,
            fragments=None,
        )


def set_download_progress_phase(
    job: Job,
    phase: str,
    *,
    reset_transfer: bool = False,
) -> None:
    global current_download_progress
    with download_progress_lock:
        progress = current_download_progress
        if progress is None or progress.job_label != job.label:
            return
        updates: dict[str, Any] = {
            "phase": phase,
            "updated_at": time.time(),
        }
        if reset_transfer:
            updates.update(
                percent=None,
                total_bytes=None,
                total_estimated=False,
                speed_bytes_per_second=None,
                eta="",
                fragment=None,
                fragments=None,
            )
        current_download_progress = replace(progress, **updates)


def update_download_progress_from_line(job: Job, mode: str, line: str) -> None:
    global current_download_progress
    track = parse_gamdl_track(line)
    transfer = parse_gamdl_transfer(line)
    if track is None and transfer is None:
        return
    with download_progress_lock:
        progress = current_download_progress
        if progress is None or progress.job_label != job.label:
            return
        updates: dict[str, Any] = {
            "mode": mode,
            "phase": "下载中",
            "updated_at": time.time(),
        }
        if track is not None:
            updates.update(
                track_index=track.index,
                track_total=track.total,
                title=track.title,
                percent=None,
                total_bytes=None,
                total_estimated=False,
                speed_bytes_per_second=None,
                eta="",
                fragment=None,
                fragments=None,
            )
        if transfer is not None:
            updates.update(
                percent=transfer.percent,
                total_bytes=transfer.total_bytes,
                total_estimated=transfer.total_estimated,
                speed_bytes_per_second=transfer.speed_bytes_per_second,
                eta=transfer.eta,
                fragment=transfer.fragment,
                fragments=transfer.fragments,
            )
        current_download_progress = replace(progress, **updates)


def clear_download_progress(job: Job | None = None) -> None:
    global current_download_progress
    with download_progress_lock:
        if (
            job is None
            or current_download_progress is None
            or current_download_progress.job_label == job.label
        ):
            current_download_progress = None


def media_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return snapshot
    for path in root.rglob("*"):
        if path.suffix.lower() not in MEDIA_OUTPUT_SUFFIXES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size <= 0:
            continue
        snapshot[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def changed_media_files(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
) -> list[Path]:
    changed = [Path(path) for path, metadata in after.items() if before.get(path) != metadata]
    return sorted(changed, key=lambda path: str(path))


def validated_verified_existing_path(line: str) -> Path | None:
    verified = parse_verified_existing_media(line)
    if verified is None:
        return None
    try:
        path = verified.path.resolve(strict=True)
        library_root = LIBRARY_ROOT.resolve(strict=True)
        path.relative_to(library_root)
        if (
            not path.is_file()
            or path.stat().st_size <= 0
            or path.suffix.lower() not in MEDIA_OUTPUT_SUFFIXES
        ):
            return None
    except (OSError, ValueError):
        return None
    return path


def path_activity_signature(root: Path) -> tuple[int, int, int]:
    file_count = 0
    total_size = 0
    latest_mtime_ns = 0
    if not root.exists():
        return file_count, total_size, latest_mtime_ns
    for path in root.rglob("*"):
        try:
            stat = path.stat()
        except OSError:
            continue
        latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
        if path.is_file():
            file_count += 1
            total_size += stat.st_size
    return file_count, total_size, latest_mtime_ns


def process_cpu_ticks(pid: int) -> tuple[int, int] | None:
    try:
        text = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        _before, after = text.rsplit(")", 1)
        fields = after.split()
        cpu_ticks = int(fields[11]) + int(fields[12])
        start_time = int(fields[19])
    except (ValueError, IndexError):
        return None
    return cpu_ticks, start_time


def process_children(pid: int) -> set[int]:
    children: set[int] = set()
    task_dir = Path("/proc") / str(pid) / "task"
    try:
        child_files = list(task_dir.glob("*/children"))
    except OSError:
        return children
    for child_file in child_files:
        try:
            text = child_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for item in text.split():
            try:
                children.add(int(item))
            except ValueError:
                continue
    return children


def process_tree_activity_signature(root_pid: int) -> tuple[int, int, int]:
    seen: set[int] = set()
    stack = [root_pid]
    total_ticks = 0
    start_time_sum = 0
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        cpu = process_cpu_ticks(pid)
        if cpu is None:
            continue
        ticks, start_time = cpu
        total_ticks += ticks
        start_time_sum += start_time
        stack.extend(process_children(pid) - seen)
    return len(seen), total_ticks, start_time_sum


def codec_for_applemusicdecrypt(mode: str) -> str:
    if mode == "atmos":
        return "ec3"
    return "alac"


def codec_priority_for_mode(mode: str, job: Job | None = None) -> str:
    if job is not None and isinstance(job.payload, dict) and "unified_music" in job.payload and mode == "alac":
        return "alac"
    if mode == "atmos":
        return "atmos,aac" if ALLOW_AAC_FALLBACK else "atmos"
    return "alac,aac" if ALLOW_AAC_FALLBACK else "alac"


def language_for_applemusicdecrypt() -> str:
    mapping = {
        "zh-cn": "zh-Hans-CN",
        "zh-hans": "zh-Hans-CN",
        "zh-hans-cn": "zh-Hans-CN",
        "zh-tw": "zh-Hant-TW",
        "zh-hant-tw": "zh-Hant-TW",
        "zh-hk": "zh-Hant-HK",
        "zh-hant-hk": "zh-Hant-HK",
    }
    return mapping.get(LANGUAGE.lower(), LANGUAGE)


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def ensure_applemusicdecrypt_config(mode: str, *, lossless_only: bool = False) -> Path:
    output_path = output_for_mode(mode)
    output_path.mkdir(parents=True, exist_ok=True)
    APPLEMUSICDECRYPT_ROOT.mkdir(parents=True, exist_ok=True)
    language = language_for_applemusicdecrypt()
    allow_aac = ALLOW_AAC_FALLBACK and not lossless_only
    codec_priority = '["alac", "ec3", "ac3", "aac"]' if allow_aac else f'["{codec_for_applemusicdecrypt(mode)}"]'
    config_text = f"""version = "0.0.10"

[instance]
url = {toml_string(WRAPPER_MANAGER_ENDPOINT)}
secure = {"true" if WRAPPER_MANAGER_SECURE else "false"}

[localInstance]
enable = false
enableHardwareAcceleration = false
hardwareAccelerator = ""
memorySize = "512M"
cpuModel = "Cascadelake-Server-v5"
showWindow = false
startArgs = "-host 0.0.0.0 -port 32767 -debug"

[region]
language = {toml_string(language)}
languageNotExistWarning = true

[download]
proxy = {toml_string(DOWNLOAD_PROXY_URL)}
parallelNum = 1
maxRunningTasks = 128
appleCDNIP = ""
codecAlternative = {"true" if allow_aac else "false"}
codecPriority = {codec_priority}
atmosConventToM4a = true
failedSongNotPassIntegrityCheck = false
audioInfoFormat = ""
songNameFormat = "{{disk}}-{{tracknum:02d}} {{title}}"
dirPathFormat = {toml_string(str(output_path / "{album_artist}" / "{album}"))}
playlistDirPathFormat = {toml_string(str(output_path / "playlists" / "{playlistName}"))}
playlistSongNameFormat = "{{playlistSongIndex:02d}}. {{artist}} - {{title}}"
saveLyrics = true
lyricsFormat = "lrc"
lyricsExtra = ["translation", "pronunciation"]
saveCover = true
coverFormat = "jpg"
coverSize = "5000x5000"
maxSampleRate = 192000
maxBitDepth = 24
afterDownloaded = ""
retryTime = 8
maxWaitTime = 30

[metadata]
embedMetadata = ["title", "artist", "album", "album_artist", "composer", "album_created",
    "genre", "created", "tracknum", "disk", "lyrics", "cover", "copyright",
    "record_company", "upc", "isrc", "rtng", "song_id", "album_id", "artist_id"]
"""
    config_path = APPLEMUSICDECRYPT_ROOT / "config.toml"
    config_path.write_text(config_text, encoding="utf-8")
    return config_path


def log_path_for(job: Job, mode: str) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(job.received_at))
    safe_chat = re.sub(r"[^0-9A-Za-z_-]+", "_", f"{job.source}_{job.chat_id}")
    return LOG_DIR / f"{timestamp}-{safe_chat}-{job.message_id}-{mode}.log"


def run_gamdl(
    job: Job,
    mode: str,
    *,
    proxy_url: str = "",
) -> tuple[int, Path, list[str], list[Path]]:
    output_path = output_for_mode(mode)
    output_path.mkdir(parents=True, exist_ok=True)
    TEMP_PATH.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_file = log_path_for(job, mode)
    tail: deque[str] = deque(maxlen=30)
    detected_failure_code = 0
    return_code = 0
    verified_existing_paths: list[Path] = []
    verified_existing_path_set: set[str] = set()
    primary_lyric_format = GAMDL_LYRIC_SIDECAR_FORMATS[0] if GAMDL_LYRIC_SIDECAR_FORMATS else "lrc"
    with log_file.open("a", encoding="utf-8") as log_handle:
        for url_index, url in enumerate(job.urls, start=1):
            set_download_progress_context(job, mode, url_index)
            before_media = media_snapshot(output_path)
            verified_existing_start = len(verified_existing_paths)
            command = [
                sys.executable,
                str(GAMDL_RUNNER),
                "--no-config-file",
                "--use-wrapper",
                "--wrapper-url",
                WRAPPER_URL,
                "--wrapper-decrypt-host",
                WRAPPER_DECRYPT_HOST,
                "--wrapper-decrypt-port",
                str(WRAPPER_DECRYPT_PORT),
                "--output-path",
                str(output_path),
                "--temp-path",
                str(TEMP_PATH),
                "--log-file",
                str(log_file),
                "--language",
                LANGUAGE,
                "--song-codec-priority",
                codec_priority_for_mode(mode, job),
                "--synced-lyrics-format",
                primary_lyric_format,
                url,
            ]
            env_vars = os.environ.copy()
            env_vars["GAMDL_USE_URL_STOREFRONT"] = "true" if GAMDL_USE_URL_STOREFRONT else "false"
            env_vars["GAMDL_LIBRARY_DEDUP_MODE"] = mode
            if proxy_url:
                for proxy_variable in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "http_proxy",
                    "https_proxy",
                ):
                    env_vars[proxy_variable] = proxy_url
            storefront = storefront_from_url(url)
            if storefront and GAMDL_USE_URL_STOREFRONT:
                env_vars["GAMDL_STOREFRONT_OVERRIDE"] = storefront
            else:
                env_vars.pop("GAMDL_STOREFRONT_OVERRIDE", None)

            env_prefix = ""
            if env_vars.get("GAMDL_STOREFRONT_OVERRIDE"):
                env_prefix = f"GAMDL_STOREFRONT_OVERRIDE={env_vars['GAMDL_STOREFRONT_OVERRIDE']} "
            if proxy_url:
                env_prefix += "GAMDL_NETWORK_PATH=nas-proxy "
            log_handle.write(f"$ {env_prefix}{' '.join(command)}\n")
            log_handle.flush()
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env_vars,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
            set_active_task_process(job, process)
            assert process.stdout is not None
            last_stdout_at = time.monotonic()
            last_activity_at = last_stdout_at
            last_activity_note_at = 0.0
            last_temp_signature = path_activity_signature(TEMP_PATH)
            last_process_signature = process_tree_activity_signature(process.pid)
            was_paused = False
            while True:
                if stop_event.is_set():
                    signal_task_process(job.task_id, signal.SIGCONT)
                    signal_task_process(job.task_id, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        signal_task_process(job.task_id, signal.SIGKILL)
                        process.wait(timeout=2)
                    break
                if task_status(job) == "paused" and process.poll() is None:
                    was_paused = True
                    stop_event.wait(0.5)
                    continue
                if was_paused:
                    last_stdout_at = last_activity_at = time.monotonic()
                    last_temp_signature = path_activity_signature(TEMP_PATH)
                    last_process_signature = process_tree_activity_signature(process.pid)
                    was_paused = False
                readable, _, _ = select.select([process.stdout], [], [], 5)
                if not readable:
                    if process.poll() is not None:
                        break
                    now = time.monotonic()
                    temp_signature = path_activity_signature(TEMP_PATH)
                    process_signature = process_tree_activity_signature(process.pid)
                    if (
                        temp_signature != last_temp_signature
                        or process_signature != last_process_signature
                    ):
                        last_temp_signature = temp_signature
                        last_process_signature = process_signature
                        last_activity_at = now
                        stdout_quiet_seconds = now - last_stdout_at
                        if (
                            stdout_quiet_seconds >= 60
                            and now - last_activity_note_at >= 60
                        ):
                            note = (
                                "[apple-music-ingest] gamdl has no new stdout for "
                                f"{int(stdout_quiet_seconds)}s, but temp files or "
                                "child process activity changed; extending watchdog."
                            )
                            log_handle.write(note + "\n")
                            log_handle.flush()
                            tail.append(note)
                            last_activity_note_at = now
                        continue
                    inactive_seconds = now - last_activity_at
                    if inactive_seconds >= GAMDL_NO_OUTPUT_TIMEOUT_SECONDS:
                        log_handle.write(
                            f"\n[apple-music-ingest] gamdl had no stdout, temp file, "
                            f"or child process activity for {int(inactive_seconds)}s; "
                            "terminating stalled process.\n"
                        )
                        log_handle.flush()
                        if not detected_failure_code:
                            detected_failure_code = 70
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        break
                    continue
                line = process.stdout.readline()
                if line == "":
                    if process.poll() is not None:
                        break
                    continue
                last_stdout_at = time.monotonic()
                last_activity_at = last_stdout_at
                log_handle.write(line)
                log_handle.flush()
                update_download_progress_from_line(job, mode, line)
                if "No downloadable media found" in line and not detected_failure_code:
                    detected_failure_code = 66
                if "Skipping " in line and "API Error" in line and not detected_failure_code:
                    detected_failure_code = 67
                if "No active Apple Music subscription" in line and not detected_failure_code:
                    detected_failure_code = 68
                if "Could not connect to the wrapper" in line and not detected_failure_code:
                    detected_failure_code = 69
                if (
                    ("[ERROR" in line or "Error downloading" in line or GAMDL_FINISHED_WITH_ERRORS_RE.search(line))
                    and not detected_failure_code
                ):
                    detected_failure_code = 72
                verified_path = validated_verified_existing_path(line)
                if (
                    verified_path is not None
                    and str(verified_path) not in verified_existing_path_set
                ):
                    verified_existing_path_set.add(str(verified_path))
                    verified_existing_paths.append(verified_path)
                tail.append(line.rstrip())
            process_code = process.wait()
            clear_active_task_process(job, process)
            after_media = media_snapshot(output_path)
            changed_files = changed_media_files(before_media, after_media)
            url_verified_existing = verified_existing_paths[verified_existing_start:]
            if changed_files:
                output_lines = [
                    f"[apple-music-ingest] detected output media: {path}"
                    for path in changed_files[:10]
                ]
                if len(changed_files) > 10:
                    output_lines.append(
                        f"[apple-music-ingest] detected {len(changed_files)} changed media files total"
                    )
                for output_line in output_lines:
                    log_handle.write(output_line + "\n")
                    tail.append(output_line)
                log_handle.flush()
            output_return_code = effective_download_return_code(
                0,
                changed_media_count=len(changed_files),
                verified_existing_count=len(url_verified_existing),
            )
            if process_code == 0 and not detected_failure_code and output_return_code:
                detected_failure_code = output_return_code
                diagnostic = (
                    "[apple-music-ingest] gamdl exited 0, but no new or modified media "
                    "file or verified existing library file was found under "
                    f"{output_path}; treating this run as failed."
                )
                log_handle.write(diagnostic + "\n")
                log_handle.flush()
                tail.append(diagnostic)
            if process_code != 0 and return_code == 0:
                return_code = detected_failure_code or process_code
            if (
                process_code == 0
                and not detected_failure_code
                and bool(changed_files)
                and not stop_event.is_set()
            ):
                set_download_progress_phase(
                    job,
                    "处理歌词与元数据",
                    reset_transfer=True,
                )
                sidecar_code = run_gamdl_lyric_sidecars(
                    job=job,
                    url=url,
                    mode=mode,
                    output_path=output_path,
                    log_handle=log_handle,
                    tail=tail,
                    env_vars=env_vars,
                    primary_lyric_format=primary_lyric_format,
                )
                if sidecar_code != 0 and return_code == 0:
                    return_code = sidecar_code
                try:
                    word_summary = sync_word_lyrics_for_paths(
                        changed_files,
                        source_url=url,
                        notification_context={
                            "source": job.source,
                            "chat_id": job.chat_id,
                            "message_id": job.message_id,
                            "task_id": job.task_id,
                        },
                        logger=lambda message: (
                            log_handle.write(message + "\n"),
                            log_handle.flush(),
                            tail.append(message),
                        ),
                    )
                    accepted_word_lyrics = int(word_summary.get("accepted", 0))
                    cached_word_lyrics = int(word_summary.get("cached", 0))
                    unavailable_word_lyrics = int(
                        word_summary.get("not_available", 0)
                    )
                    rejected_word_lyrics = int(word_summary.get("rejected", 0))
                    errored_word_lyrics = int(word_summary.get("error", 0))
                    queued_word_lyrics = int(word_summary.get("queued", 0))
                    word_note = (
                        "[apple-word-lyrics] summary "
                        f"accepted={accepted_word_lyrics} "
                        f"cached={cached_word_lyrics} "
                        f"not_available={unavailable_word_lyrics} "
                        f"queued={queued_word_lyrics} "
                        f"rejected={rejected_word_lyrics} "
                        f"error={errored_word_lyrics}"
                    )
                    log_handle.write(word_note + "\n")
                    log_handle.flush()
                    tail.append(word_note)
                    if accepted_word_lyrics or word_summary.get("character_normalized"):
                        trigger_navidrome_scan()
                except Exception as exc:
                    word_note = (
                        "[apple-word-lyrics] non-fatal sync failure: "
                        f"{type(exc).__name__}"
                    )
                    log_handle.write(word_note + "\n")
                    log_handle.flush()
                    tail.append(word_note)
    if return_code == 0 and detected_failure_code:
        return_code = detected_failure_code
    return return_code, log_file, list(tail), verified_existing_paths


def run_gamdl_lyric_sidecars(
    *,
    job: Job,
    url: str,
    mode: str,
    output_path: Path,
    log_handle: Any,
    tail: deque[str],
    env_vars: dict[str, str],
    primary_lyric_format: str,
) -> int:
    if not GAMDL_LYRIC_SIDECAR_FORMATS:
        return 0

    return_code = 0
    for lyric_format in GAMDL_LYRIC_SIDECAR_FORMATS:
        if lyric_format == primary_lyric_format and not GAMDL_LYRIC_SIDECAR_OVERWRITE:
            continue
        attempts = max(1, GAMDL_LYRIC_SIDECAR_RETRIES + 1)
        format_return_code = 0
        for attempt in range(1, attempts + 1):
            command = [
                sys.executable,
                str(GAMDL_RUNNER),
                "--no-config-file",
                "--use-wrapper",
                "--wrapper-url",
                WRAPPER_URL,
                "--wrapper-decrypt-host",
                WRAPPER_DECRYPT_HOST,
                "--wrapper-decrypt-port",
                str(WRAPPER_DECRYPT_PORT),
                "--output-path",
                str(output_path),
                "--temp-path",
                str(TEMP_PATH),
                "--log-file",
                str(log_handle.name),
                "--language",
                LANGUAGE,
                "--song-codec-priority",
                codec_priority_for_mode(mode, job),
                "--synced-lyrics-only",
                "--synced-lyrics-format",
                lyric_format,
            ]
            if GAMDL_LYRIC_SIDECAR_OVERWRITE:
                command.append("--overwrite")
            command.append(url)

            note = f"[apple-music-ingest] fetching {lyric_format.upper()} sidecar lyrics"
            if attempt > 1:
                note += f" retry {attempt}/{attempts}"
            log_handle.write(note + "\n")
            log_handle.write(f"$ {' '.join(command)}\n")
            log_handle.flush()
            tail.append(note)

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env_vars,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
            set_active_task_process(job, process)
            assert process.stdout is not None
            last_output_at = time.monotonic()
            format_return_code = 0
            while True:
                readable, _, _ = select.select([process.stdout], [], [], 5)
                if readable:
                    line = process.stdout.readline()
                    if line == "" and process.poll() is not None:
                        break
                    if line:
                        log_handle.write(line)
                        log_handle.flush()
                        tail.append(line.rstrip())
                    last_output_at = time.monotonic()
                    continue

                if process.poll() is not None:
                    break
                if time.monotonic() - last_output_at >= GAMDL_LYRIC_SIDECAR_TIMEOUT_SECONDS:
                    timeout_note = (
                        f"[apple-music-ingest] {lyric_format.upper()} sidecar lyrics "
                        f"timed out after {GAMDL_LYRIC_SIDECAR_TIMEOUT_SECONDS}s"
                    )
                    log_handle.write(timeout_note + "\n")
                    log_handle.flush()
                    tail.append(timeout_note)
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    format_return_code = 73
                    break

            process_code = process.wait()
            clear_active_task_process(job, process)
            if process_code != 0 and format_return_code == 0:
                format_return_code = process_code
            if format_return_code == 0:
                break
            if attempt < attempts:
                retry_note = (
                    f"[apple-music-ingest] {lyric_format.upper()} sidecar lyrics "
                    f"failed with code {format_return_code}; retrying in "
                    f"{GAMDL_LYRIC_SIDECAR_RETRY_DELAY_SECONDS:g}s"
                )
                log_handle.write(retry_note + "\n")
                log_handle.flush()
                tail.append(retry_note)
                time.sleep(max(0.0, GAMDL_LYRIC_SIDECAR_RETRY_DELAY_SECONDS))

        if format_return_code != 0 and return_code == 0:
            return_code = format_return_code

    return return_code


def run_applemusicdecrypt(job: Job, mode: str) -> tuple[int, Path, list[str], list[Path]]:
    TEMP_PATH.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ensure_applemusicdecrypt_config(mode, lossless_only=bool(isinstance(job.payload, dict) and "unified_music" in job.payload))

    log_file = log_path_for(job, mode)
    command = [
        sys.executable,
        str(APPLEMUSICDECRYPT_RUNNER),
        "--codec",
        codec_for_applemusicdecrypt(mode),
        "--language",
        language_for_applemusicdecrypt(),
        *job.urls,
    ]

    tail: deque[str] = deque(maxlen=30)
    env_vars = os.environ.copy()
    env_vars["APPLEMUSICDECRYPT_ROOT"] = str(APPLEMUSICDECRYPT_ROOT)
    set_download_progress_context(
        job,
        mode,
        1,
        phase="下载中（当前后端不提供百分比）",
    )
    with log_file.open("a", encoding="utf-8") as log_handle:
        log_handle.write(f"$ {' '.join(command)}\n")
        log_handle.flush()
        process = subprocess.Popen(
            command,
            cwd=str(APPLEMUSICDECRYPT_ROOT),
            env=env_vars,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        set_active_task_process(job, process)
        assert process.stdout is not None
        for line in process.stdout:
            log_handle.write(line)
            log_handle.flush()
            tail.append(line.rstrip())
        return_code = process.wait()
        clear_active_task_process(job, process)
    return return_code, log_file, list(tail), []


def run_download(
    job: Job,
    mode: str,
    *,
    proxy_url: str = "",
) -> tuple[int, Path, list[str], list[Path]]:
    if BACKEND == "applemusicdecrypt":
        return run_applemusicdecrypt(job, mode)
    return run_gamdl(job, mode, proxy_url=proxy_url)


def log_has_pattern(log_file: Path, tail: list[str], pattern: re.Pattern[str]) -> bool:
    if any(pattern.search(line) for line in tail):
        return True
    try:
        text = log_file.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        log(f"could not inspect gamdl log for retryable errors: {exc}")
        return False
    return pattern.search(text) is not None


def is_wrapper_m3u8_failure(log_file: Path, tail: list[str]) -> bool:
    return log_has_pattern(log_file, tail, WRAPPER_M3U8_FAILURE_RE)


def is_transient_gamdl_failure(log_file: Path, tail: list[str]) -> bool:
    return log_has_pattern(log_file, tail, TRANSIENT_GAMDL_FAILURE_RE)


def deduplicate_apple_music_urls(
    urls: list[str],
    mode: str,
) -> tuple[list[str], list[ExistingTrack]]:
    try:
        stats = refresh_apple_music_library_index()
        log(
            "Apple Music library index refreshed "
            f"files={stats['files']} parsed={stats['parsed']} "
            f"removed={stats['removed']} tracks={stats['tracks']} "
            f"isrc_conflicts={stats.get('isrc_conflicts', 0)}"
        )
    except Exception as exc:
        log(f"Apple Music library index refresh failed; continuing without precheck: {exc}")
        return urls, []

    pending: list[str] = []
    existing_tracks: list[ExistingTrack] = []
    for url in urls:
        song_id = apple_music_song_id_from_url(url)
        if not song_id:
            pending.append(url)
            continue
        existing = find_existing_track(song_id, mode)
        if existing is None:
            pending.append(url)
            continue
        existing_tracks.append(existing)
    return pending, existing_tracks


def docker_socket_request(method: str, path: str, timeout: float) -> int:
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: docker\r\n"
        "Content-Length: 0\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(WRAPPER_RECOVERY_DOCKER_SOCKET)
        client.sendall(request)
        response = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response += chunk
            if b"\r\n\r\n" in response and len(response) > 8192:
                break
    status_line = response.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    parts = status_line.split()
    if len(parts) < 2 or not parts[1].isdigit():
        raise RuntimeError(f"unexpected Docker API response: {status_line!r}")
    return int(parts[1])


def recover_wrapper_m3u8(reason: str) -> bool:
    global last_wrapper_recovery_at
    if not WRAPPER_RECOVERY_ENABLED:
        log(f"wrapper recovery disabled; reason={reason}")
        return False
    if not Path(WRAPPER_RECOVERY_DOCKER_SOCKET).exists():
        log(f"wrapper recovery skipped; Docker socket is missing: {WRAPPER_RECOVERY_DOCKER_SOCKET}")
        return False

    with wrapper_recovery_lock:
        now = time.monotonic()
        elapsed = now - last_wrapper_recovery_at
        if elapsed < WRAPPER_RECOVERY_MIN_INTERVAL_SECONDS:
            remaining = int(WRAPPER_RECOVERY_MIN_INTERVAL_SECONDS - elapsed)
            log(f"wrapper recovery throttled for {remaining}s; reason={reason}")
            return wait_for_wrapper_ports(WRAPPER_RECOVERY_WAIT_SECONDS)

        container_names = [
            item.strip()
            for item in WRAPPER_RECOVERY_DOCKER_CONTAINER.split(",")
            if item.strip()
        ]
        if not container_names:
            log("wrapper recovery skipped; no Docker containers are configured")
            return False
        for container_name in container_names:
            container = urllib.parse.quote(container_name, safe="")
            path = f"/containers/{container}/restart?t={WRAPPER_RECOVERY_TIMEOUT_SECONDS}"
            log(f"restarting wrapper container {container_name}; reason={reason}")
            try:
                status = docker_socket_request(
                    "POST",
                    path,
                    timeout=WRAPPER_RECOVERY_TIMEOUT_SECONDS + 10,
                )
            except Exception as exc:
                log(f"wrapper recovery Docker restart failed for {container_name}: {exc}")
                return False
            if status not in {204, 304}:
                log(
                    f"wrapper recovery Docker restart for {container_name} "
                    f"returned HTTP {status}"
                )
                return False
        last_wrapper_recovery_at = time.monotonic()

    ready = wait_for_wrapper_ports(WRAPPER_RECOVERY_WAIT_SECONDS)
    if ready:
        log("wrapper recovery completed; wrapper ports are ready")
    else:
        log("wrapper recovery timed out waiting for wrapper ports")
    return ready


def process_online_media_job(job: Job) -> None:
    if job.kind == "youtube_audio":
        kind_name = "YouTube Music"
        downloader = download_youtube
        display_path = display_youtube_path
    elif job.kind == "youtube_video":
        kind_name = "YouTube 视频"
        downloader = download_youtube
        display_path = display_youtube_path
    elif job.kind == "bilibili_audio":
        kind_name = "哔哩哔哩音乐"
        downloader = download_bilibili
        display_path = display_bilibili_path
    elif job.kind == "bilibili_video":
        kind_name = "哔哩哔哩视频"
        downloader = download_bilibili
        display_path = display_bilibili_path
    else:
        raise ValueError(f"unsupported online media job kind: {job.kind}")
    try:
        observer = lambda process: online_process_observer(job, process)
        progress_observer = lambda progress: online_progress_observer(job, progress)
        download_options = {
            "process_observer": observer,
            "progress_observer": progress_observer,
        }
        if job.kind in {"youtube_audio", "youtube_video"}:
            download_options["cancel_observer"] = (
                lambda: stop_event.is_set() or task_should_stop(job)
            )
        with online_video_library_lock:
            result = downloader(
                job.kind,
                job.urls[0],
                job.label,
                LOG_DIR,
                **download_options,
            )
    except Exception as exc:
        clear_active_task_process(job)
        error_text = str(exc)
        if task_status(job) in {"stopping", "stopped"}:
            update_task_card(
                job.task_id,
                force=True,
                status="stopped",
                phase="已停止",
                finished_at=time.time(),
                speed_bps=None,
                eta=None,
                error="",
            )
            return
        append_journal(
            {
                "event": "finished",
                "job": job_record(job),
                "time": time.time(),
                "failures": [{"kind": job.kind, "error": error_text}],
            }
        )
        update_task_card(
            job.task_id,
            force=True,
            status="failed",
            phase="下载失败",
            finished_at=time.time(),
            speed_bps=None,
            eta=None,
            error=redact_sensitive_text(error_text),
        )
        send_reply(job, f"{kind_name} 下载失败。\n原因：{error_text[:3200]}")
        return

    clear_active_task_process(job)
    online_word_summary: dict[str, int] = {}
    if job.kind in {"youtube_audio", "bilibili_audio"}:
        lyric_paths = unique_path_strings(
            [*result.added_paths, *result.existing_paths]
        )
        try:
            online_word_summary = sync_word_lyrics_for_paths(
                lyric_paths,
                source_url=job.urls[0],
                notification_context={
                    "source": job.source,
                    "chat_id": job.chat_id,
                    "message_id": job.message_id,
                    "task_id": job.task_id,
                },
                logger=log,
            )
            if int(online_word_summary.get("accepted", 0)):
                trigger_navidrome_scan()
        except Exception as exc:
            log(
                "[word-lyrics-automation] online-media sync failed "
                f"kind={job.kind} error={type(exc).__name__}:{exc}"
            )
    added_entries, added_bytes = sized_paths(result.added_paths)
    existing_entries, existing_bytes = sized_paths(result.existing_paths)
    append_journal(
        {
            "event": "finished",
            "job": job_record(job),
            "time": time.time(),
            "added_paths": result.added_paths,
            "existing_paths": result.existing_paths,
            "added_bytes": added_bytes,
            "existing_bytes": existing_bytes,
            "metadata_matches": result.metadata_matches,
            "lyrics_added": result.lyrics_added,
            "scan_triggered": result.scan_triggered,
            "failures": [],
        }
    )
    completed_bytes = added_bytes + existing_bytes
    task = task_card_store.get(job.task_id) if job.task_id > 0 else None
    total_bytes = completed_bytes or (task.get("total_bytes") if task else None)
    completion_paths = unique_path_strings(
        [*result.added_paths, *result.existing_paths]
    )
    delete_paths = deletion_targets_for_media(job.kind, result.added_paths)
    complete_task_card(
        job.task_id,
        status="completed",
        phase="入库完成" if result.added_count else "媒体库已存在",
        title=result.title,
        finished_at=time.time(),
        percent=100.0,
        downloaded_bytes=total_bytes,
        total_bytes=total_bytes,
        total_estimated=0,
        speed_bps=None,
        eta="00:00",
        error="",
        local_paths_json=json.dumps(completion_paths, ensure_ascii=False),
        delete_paths_json=json.dumps(delete_paths, ensure_ascii=False),
    )
    if result.added_count:
        heading = f"{kind_name} 入库完成"
    else:
        heading = f"{kind_name} 已存在，没有重复入库"
    lines = [
        heading,
        f"标题：{truncate_display_text(result.title, 100)}",
        (
            f"新增：{result.added_count}"
            + (f"（{format_file_size(added_bytes)}）" if result.added_count else "")
            + f"，已存在：{result.existing_count}"
            + (f"（{format_file_size(existing_bytes)}）" if result.existing_count else "")
        ),
    ]
    if job.kind in {"youtube_audio", "bilibili_audio"}:
        lines.append(f"MusicBrainz/beets 高置信度匹配：{result.metadata_matches}")
        lines.append(f"LRC 歌词：{result.lyrics_added}")
        if online_word_summary:
            lines.append("逐词歌词：由 Bot 逐首通知结果，剩余数量见实时队列消息。")
            if int(online_word_summary.get("character_normalized", 0)):
                lines.append("汉字已逐字细分；单字边界在原词组时间内估算。")
            if int(online_word_summary.get("character_errors", 0)):
                lines.append("部分逐字细分失败，原歌词已保留。")
            if int(online_word_summary.get("queued", 0)):
                lines.append(
                    "无词录音经核验后结案；暂缺来源会通知等待原因和下次重查时间，随后每六小时重查。有最终结果时，本 Bot 会主动通知。"
                )
        lines.append(f"Navidrome 扫描：{'已触发' if result.scan_triggered else '未触发，将由定时扫描发现'}")
    else:
        lines.append("本地元数据：NFO、封面、中文字幕/英文字幕（存在时）")
        lines.append(f"Emby 扫描：{'已触发' if result.scan_triggered else '未触发，将由定时扫描发现'}")
        lines.append(
            f"Emby 封面与源链接：已验证 {getattr(result, 'emby_metadata_synced', 0)} 项"
        )
    for label, entries in (("新增文件", added_entries), ("已存在文件", existing_entries)):
        if not entries:
            continue
        lines.append(f"{label}：")
        lines.extend(
            f"[{format_file_size(size)}] {display_path(path, job.kind)}"
            for path, size in entries[:3]
        )
        if len(entries) > 3:
            lines.append(f"另有 {len(entries) - 3} 项")
    send_reply(job, "\n".join(lines))


def process_job(job: Job) -> None:
    global current_job
    with current_job_lock:
        current_job = job

    if not wait_for_task_ready(job):
        if preserve_job_for_restart(job):
            with current_job_lock:
                current_job = None
            return
        update_task_card(
            job.task_id,
            force=True,
            status="stopped",
            phase="已停止",
            finished_at=time.time(),
            speed_bps=None,
            eta=None,
        )
        append_journal(
            {
                "event": "finished",
                "job": job_record(job),
                "time": time.time(),
                "failures": [],
                "stopped": True,
            }
        )
        with current_job_lock:
            current_job = None
        return

    task = task_card_store.get(job.task_id) if job.task_id > 0 else None
    update_task_card(
        job.task_id,
        force=True,
        status="running",
        phase="准备下载",
        started_at=(task.get("started_at") if task else None) or time.time(),
        speed_bps=None,
        eta=None,
    )
    append_journal(
        {
            "event": "started",
            "job": job_record(job),
            "time": time.time(),
        }
    )
    if (job.kind == "apple_music" and job.mode == "alac" and len(job.urls) == 1
            and isinstance(job.payload, dict) and "unified_music" in job.payload):
        process_unified_music_job(job)
        with current_job_lock:
            current_job = None
        return
    if job.kind == "lx_music":
        process_lx_music_job(job)
        with current_job_lock:
            current_job = None
        return
    if job.kind in {
        "youtube_audio",
        "youtube_video",
        "bilibili_audio",
        "bilibili_video",
    }:
        process_online_media_job(job)
        with current_job_lock:
            current_job = None
        return

    send_reply(
        job,
        f"开始下载 {len(job.urls)} 个 Apple Music 链接，模式：{job.mode}",
    )

    modes = ["alac", "atmos"] if job.mode == "both" else [job.mode]
    failures = []
    fallbacks: list[LxFallbackOutcome] = []
    completion_paths: list[str | Path] = []
    completion_delete_paths: list[str | Path] = []
    produced_media = False
    verified_existing_media = False
    for mode in modes:
        if stop_event.is_set() or task_should_stop(job):
            break
        if not wait_for_task_ready(job):
            break
        pending_urls, existing_tracks = deduplicate_apple_music_urls(job.urls, mode)
        completion_paths.extend(track.path for track in existing_tracks)
        verified_existing_media = verified_existing_media or bool(existing_tracks)
        if existing_tracks:
            paths = "\n".join(
                f"- {track.display_path}"
                for track in existing_tracks[:10]
            )
            extra = (
                f"\n另有 {len(existing_tracks) - 10} 首已存在。"
                if len(existing_tracks) > 10
                else ""
            )
            send_reply(
                job,
                f"{mode.upper()} 媒体库已存在，跳过 {len(existing_tracks)} 首：\n"
                f"{paths}{extra}",
            )
        if not pending_urls:
            log(
                f"job {job.label} mode={mode} skipped; "
                f"all {len(existing_tracks)} track(s) already exist"
            )
            continue
        output_path = output_for_mode(mode)
        mode_before_media = media_snapshot(output_path)
        mode_job = replace(job, urls=pending_urls)
        begin_download_progress(mode_job, mode)
        log(f"job {job.label} running mode={mode} urls={len(pending_urls)}")
        retry_index = 0
        retry_proxy_url = ""
        while True:
            if not wait_for_task_ready(job):
                break
            return_code, log_file, tail, run_existing_paths = run_download(
                mode_job,
                mode,
                proxy_url=retry_proxy_url,
            )
            wrapper_failure = return_code == 72 and is_wrapper_m3u8_failure(log_file, tail)
            transient_failure = return_code == 72 and is_transient_gamdl_failure(log_file, tail)
            retry_limit = (
                WRAPPER_M3U8_FAILURE_JOB_RETRIES
                if wrapper_failure
                else TRANSIENT_FAILURE_JOB_RETRIES
            )
            if (
                return_code == 72
                and retry_index < retry_limit
                and (wrapper_failure or transient_failure)
                and not stop_event.is_set()
            ):
                retry_index += 1
                recovered = False
                if wrapper_failure:
                    recovered = recover_wrapper_m3u8(f"job={job.label} mode={mode}")
                    delay = 5 if recovered else WRAPPER_M3U8_FAILURE_JOB_RETRY_DELAY_SECONDS
                    reason = "wrapper m3u8 failure"
                    recovery_text = "已重启 wrapper" if recovered else "wrapper 自动恢复未完成"
                    reply_reason = f"获取 wrapper m3u8 失败，{recovery_text}"
                else:
                    delay = TRANSIENT_FAILURE_JOB_RETRY_DELAY_SECONDS
                    reason = "transient gamdl network/decrypt failure"
                    reply_reason = "检测到 Apple Music/gamdl 临时网络或解密链路中断"
                    if DOWNLOAD_PROXY_URL and not retry_proxy_url:
                        retry_proxy_url = DOWNLOAD_PROXY_URL
                        reason += "; switching to NAS proxy"
                        reply_reason += "，下一次尝试切换 NAS 代理路径"
                log(
                    f"job {job.label} mode={mode} hit {reason}; "
                    f"retrying whole download in {int(delay)}s "
                    f"({retry_index}/{retry_limit})"
                )
                send_reply(
                    job,
                    f"{mode.upper()} {reply_reason}，等待 {int(delay)} 秒后自动重试 "
                    f"{retry_index}/{retry_limit}。\n日志：{log_file}",
                )
                set_download_progress_phase(
                    mode_job,
                    f"等待自动重试 {retry_index}/{retry_limit}",
                    reset_transfer=True,
                )
                time.sleep(delay)
                continue
            break
        if stop_event.is_set() or task_should_stop(job):
            break
        mode_after_media = media_snapshot(output_path)
        changed_paths = changed_media_files(mode_before_media, mode_after_media)
        return_code = effective_download_return_code(
            return_code,
            changed_media_count=len(changed_paths),
            verified_existing_count=len(run_existing_paths),
        )
        if return_code == 0:
            created_paths = [
                Path(path)
                for path in mode_after_media
                if path not in mode_before_media
            ]
            completion_paths.extend(changed_paths)
            completion_paths.extend(run_existing_paths)
            produced_media = produced_media or bool(changed_paths)
            verified_existing_media = verified_existing_media or bool(run_existing_paths)
            completion_delete_paths.extend(
                deletion_targets_for_media("apple_music", created_paths)
            )
            set_download_progress_phase(
                mode_job,
                "本格式下载完成" if changed_paths else "媒体库已存在",
                reset_transfer=True,
            )
            if changed_paths:
                existing_note = (
                    f"，另有 {len(run_existing_paths)} 首已存在"
                    if run_existing_paths
                    else ""
                )
                send_reply(
                    job,
                    f"{mode.upper()} 下载完成{existing_note}。日志：{log_file}",
                )
            else:
                send_reply(
                    job,
                    f"{mode.upper()} 媒体库已存在，未重复下载。日志：{log_file}",
                )
        else:
            try:
                fallback = run_lx_fallback(
                    job,
                    mode_job,
                    mode,
                    return_code,
                    log_file,
                    tail,
                )
            except LxFallbackStopped:
                break
            if fallback is not None:
                fallbacks.append(fallback)
                if fallback.success:
                    completion_paths.extend(fallback.paths)
                    produced_media = True
                    completion_delete_paths.extend(
                        deletion_targets_for_media("apple_music", list(fallback.paths))
                    )
                    scan_text = (
                        "已触发"
                        if fallback.scan_triggered
                        else "未触发，将由 Navidrome 定时扫描发现"
                    )
                    send_reply(
                        job,
                        "Apple Music 确认无可下载媒体，LX 高品质音源兜底完成。\n"
                        f"曲目：{fallback.artist} - {fallback.title}\n"
                        "音质："
                        + (
                            "320 kbps MP3"
                            if fallback.quality.casefold() == MP3_320_QUALITY
                            else (
                                f"{fallback.bit_depth}bit/"
                                f"{fallback.sample_rate / 1000:g}kHz FLAC"
                            )
                        )
                        + "\n"
                        f"Navidrome 扫描：{scan_text}",
                    )
                    continue
                send_reply(
                    job,
                    "Apple Music 确认无可下载媒体，但 LX 高品质音源兜底也未完成。\n"
                    f"原因：{fallback.message}",
                )
            set_download_progress_phase(
                mode_job,
                "下载失败",
                reset_transfer=True,
            )
            failures.append((mode, return_code, log_file, tail))
            tail_text = "\n".join(tail[-12:]) or "无输出"
            send_reply(
                job,
                f"{mode.upper()} 下载失败，退出码 {return_code}。\n日志：{log_file}\n\n最后输出：\n{tail_text}",
            )

    if preserve_job_for_restart(job):
        clear_download_progress(job)
        with current_job_lock:
            current_job = None
        return
    stopped = task_should_stop(job)
    completion_path_strings = unique_path_strings(completion_paths)
    no_output = not stopped and not failures and not completion_path_strings
    append_journal(
        {
            "event": "finished",
            "job": job_record(job),
            "time": time.time(),
            "failures": [
                {"mode": mode, "return_code": code, "log_file": str(path)}
                for mode, code, path, _tail in failures
            ],
            "fallbacks": [
                {
                    "success": fallback.success,
                    "signature": fallback.signature,
                    "title": fallback.title,
                    "artist": fallback.artist,
                    "album": fallback.album,
                    "queue_id": fallback.queue_id,
                    "paths": [str(path) for path in fallback.paths],
                    "scan_triggered": fallback.scan_triggered,
                    "quality": fallback.quality,
                    "sample_rate": fallback.sample_rate,
                    "bit_depth": fallback.bit_depth,
                    "message": fallback.message,
                }
                for fallback in fallbacks
            ],
            "stopped": stopped,
            "no_output": no_output,
        }
    )
    if stopped:
        update_task_card(
            job.task_id,
            force=True,
            status="stopped",
            phase="已停止",
            finished_at=time.time(),
            speed_bps=None,
            eta=None,
            error="",
        )
    elif failures:
        mode, code, path, tail = failures[0]
        error = "\n".join(tail[-5:]) or f"{mode.upper()} 退出码 {code}"
        update_task_card(
            job.task_id,
            force=True,
            status="failed",
            phase="下载失败",
            finished_at=time.time(),
            speed_bps=None,
            eta=None,
            error=f"{redact_sensitive_text(error)}\n日志：{path}",
        )
    elif no_output:
        error = "下载器没有生成媒体文件，也没有找到可信的已存在文件。"
        update_task_card(
            job.task_id,
            force=True,
            status="failed",
            phase="未生成媒体文件",
            finished_at=time.time(),
            speed_bps=None,
            eta=None,
            error=error,
        )
        send_reply(job, f"Apple Music 下载失败：{error}")
    else:
        task = task_card_store.get(job.task_id) if job.task_id > 0 else None
        completed_total = task.get("total_bytes") if task else None
        complete_task_card(
            job.task_id,
            status="completed",
            phase=(
                "LX 高品质音源兜底完成"
                if any(fallback.success for fallback in fallbacks)
                else (
                    "下载完成"
                    if produced_media
                    else (
                        "媒体库已存在"
                        if verified_existing_media
                        else "下载完成"
                    )
                )
            ),
            finished_at=time.time(),
            percent=100.0,
            downloaded_bytes=completed_total,
            speed_bps=None,
            eta="00:00",
            error="",
            local_paths_json=json.dumps(completion_path_strings, ensure_ascii=False),
            delete_paths_json=json.dumps(
                unique_path_strings(completion_delete_paths),
                ensure_ascii=False,
            ),
        )
    clear_download_progress(job)
    with current_job_lock:
        current_job = None


def worker_loop() -> None:
    global current_job
    wait_for_backend()
    while not stop_event.is_set():
        try:
            job = job_queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            process_job(job)
        except Exception as exc:
            if preserve_job_for_restart(job):
                continue
            log(f"job {job.label} crashed: {exc}")
            append_journal(
                {
                    "event": "crashed",
                    "job": job_record(job),
                    "time": time.time(),
                    "error": redact_sensitive_text(repr(exc)),
                }
            )
            update_task_card(
                job.task_id,
                force=True,
                status="failed",
                phase="任务异常退出",
                finished_at=time.time(),
                speed_bps=None,
                eta=None,
                error=redact_sensitive_text(exc),
            )
            send_reply(job, f"任务异常退出：{redact_sensitive_text(exc)}")
        finally:
            clear_active_task_process(job)
            clear_download_progress(job)
            with current_job_lock:
                if current_job == job:
                    current_job = None
            job_queue.task_done()


def recover_task_card_jobs() -> int:
    recovered = 0
    for task in task_card_store.recover_after_restart():
        try:
            raw = task_card_store.job_from_task(task)
            job = Job(
                source=str(raw["source"]),
                chat_id=str(raw["chat_id"]),
                message_id=str(raw["message_id"]),
                urls=[str(url) for url in raw["urls"]],
                mode=str(raw["mode"]),
                kind=str(raw["kind"]),
                received_at=float(raw["received_at"]),
                task_id=int(task["id"]),
                payload=(
                    dict(raw["payload"])
                    if isinstance(raw.get("payload"), dict)
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            log(f"could not recover task card {task.get('id')}: {exc}")
            task_card_store.update(
                int(task["id"]),
                status="failed",
                phase="恢复失败",
                finished_at=time.time(),
                error=f"容器重启后无法恢复任务：{exc}",
            )
            update_task_card_message(int(task["id"]), force=True)
            continue
        job_queue.put(job)
        update_task_card_message(job.task_id, force=True)
        recovered += 1
    if recovered:
        log(f"recovered {recovered} queued task card(s) after restart")
    return recovered


def queue_status() -> str:
    with current_job_lock:
        running = current_job
    running_text = f"{running.kind} / {running.label}" if running else "无"
    return f"当前任务：{running_text}\n排队数量：{job_queue.qsize()}"


def format_elapsed_time(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def download_progress_status() -> str:
    with current_job_lock:
        running = current_job
    with download_progress_lock:
        progress = current_download_progress
    queued = job_queue.qsize()

    if running is None:
        return f"当前没有正在下载的任务。\n排队数量：{queued}"
    if progress is None or progress.job_label != running.label:
        return (
            f"当前任务：{running.kind}\n"
            "该任务目前没有可读取的实时下载进度。\n"
            f"排队数量：{queued}"
        )

    lines = [
        "统一无损下载进度" if isinstance(running.payload, dict) and "unified_music" in running.payload else "Apple Music 下载进度",
        f"格式：{progress.mode.upper()} · 阶段：{progress.phase}",
    ]
    if progress.title:
        lines.append(f"曲目：{truncate_display_text(progress.title, 100)}")
    if progress.percent is not None:
        lines.append(f"{progress_bar(progress.percent)} {progress.percent:.1f}%")
        if progress.total_bytes is not None:
            downloaded = progress.total_bytes * progress.percent / 100
            total_prefix = "约 " if progress.total_estimated else ""
            lines.append(
                f"已下载：{format_iec_size(downloaded)} / "
                f"{total_prefix}{format_iec_size(progress.total_bytes)}"
            )
        if progress.speed_bytes_per_second is not None:
            stale = time.time() - progress.updated_at > 15
            speed_label = "最近速度" if stale else "速度"
            lines.append(
                f"{speed_label}："
                f"{format_iec_size(progress.speed_bytes_per_second)}/s"
            )
        if progress.eta and progress.eta.lower() != "unknown":
            lines.append(f"预计剩余：{progress.eta}")
    if progress.track_total:
        lines.append(f"曲目序号：{progress.track_index}/{progress.track_total}")
    if progress.url_total > 1:
        lines.append(f"链接序号：{progress.url_index}/{progress.url_total}")
    if progress.fragment is not None and progress.fragments:
        lines.append(f"分片：{progress.fragment}/{progress.fragments}")
    lines.append(
        f"本格式用时：{format_elapsed_time(time.time() - progress.started_at)}"
    )
    lines.append(f"排队数量：{queued}")
    return "\n".join(lines)


def help_text() -> str:
    lines = [
        ("发送歌名会统一搜索 Apple、LX、SQ，点按钮按 Apple → LX → SQ 尝试无损下载，结果支持翻页。"
         if UNIFIED_MUSIC_ENABLED else "发送歌名会搜索 Apple Music，点按钮即可下载，搜索结果支持上下翻页。"),
        "发送 Apple Music 链接仍会直接下载。",
        ("/alac 歌名或单曲链接：Apple 优先，LX、SQ 补充无损" if UNIFIED_MUSIC_ENABLED else "/alac 歌名或链接：下载 ALAC"),
        "/atmos 歌名或链接：下载 Atmos",
        "/both 歌名或链接：两个格式都下载",
        "/search 歌名：只搜索并选择",
    ]
    if YOUTUBE_ENABLED:
        lines.extend(
            [
                "发送 YouTube Music 单曲链接：自动下载音乐并入库 Navidrome",
                "发送普通 YouTube 链接：选择音乐入库或视频入库",
                "/ytmusic YouTube链接：直接按音乐下载",
                "/ytvideo YouTube链接：直接按视频下载",
            ]
        )
    if BILIBILI_ENABLED:
        lines.extend(
            [
                "发送哔哩哔哩链接：选择音乐入库或视频入库",
                "/biliaudio 哔哩哔哩链接：直接按音乐下载",
                "/bilivideo 哔哩哔哩链接：直接按视频下载",
            ]
        )
    if YOUTUBE_ENABLED or BILIBILI_ENABLED:
        lines.append("/deletevideo：分页选择并安全删除在线视频")
    lines.extend(
        [
            "/status：查看队列",
            "/progress：查看当前下载进度和速度",
            "群组开启隐私模式时，请使用上述命令或明确提及 Bot。",
        ]
    )
    return "\n".join(lines)


def handle_text(
    source: str,
    chat_id: str,
    text: str,
    message_id: str,
    actor_id: str = "",
) -> str | None:
    stripped = text.strip()
    first_token = stripped.split(maxsplit=1)[0].lower() if stripped else ""
    command = first_token.split("@", 1)[0]
    if command in {"/deletevideo", "/delvideo"} or stripped == "删除视频":
        if source != "telegram":
            return "视频删除菜单目前只支持 Telegram。"
        if send_video_delete_menu(chat_id, actor_id):
            return None
        return "当前没有可删除的 YouTube / 哔哩哔哩视频。"
    if stripped.startswith("/status") or stripped.lower() == "status":
        return queue_status()
    if command in {"/progress", "/speed"} or stripped in {"进度", "下载进度"}:
        return download_progress_status()
    if stripped.startswith("/help") or stripped == "/start" or stripped.lower() == "help":
        return help_text()

    apple_urls = extract_urls(text)
    youtube_urls = extract_youtube_urls(text)
    bilibili_urls = extract_bilibili_urls(text)
    youtube_kind = youtube_command_kind(text)
    bilibili_kind = bilibili_command_kind(text)
    platform_count = sum(bool(urls) for urls in (apple_urls, youtube_urls, bilibili_urls))
    if platform_count > 1:
        return "请把 Apple Music、YouTube 和哔哩哔哩链接分成不同消息发送。"
    if youtube_urls:
        if not YOUTUBE_ENABLED:
            return "YouTube 下载功能目前未启用。"
        if len(youtube_urls) > 1:
            return "为了避免误下大型播放列表，请每条消息只发送一个 YouTube 链接。"
        youtube_url = youtube_urls[0]
        if youtube_kind:
            return queue_download(
                source,
                chat_id,
                message_id,
                [youtube_url],
                "audio" if youtube_kind == "youtube_audio" else "video",
                youtube_kind,
            )
        if is_youtube_music_url(youtube_url):
            return queue_download(
                source,
                chat_id,
                message_id,
                [youtube_url],
                "audio",
                "youtube_audio",
            )
        if source != "telegram":
            return "普通 YouTube 链接需要选择类型；请使用 /ytmusic 链接 或 /ytvideo 链接。"
        try:
            send_youtube_prompt(chat_id, actor_id, youtube_url)
        except YouTubeIngestError as exc:
            return f"YouTube 链接读取失败：{exc}"
        return None
    if youtube_kind:
        return "命令后面需要附上 YouTube 或 YouTube Music 链接。"

    if bilibili_urls:
        if not BILIBILI_ENABLED:
            return "哔哩哔哩下载功能目前未启用。"
        if len(bilibili_urls) > 1:
            return "为了避免误下大型列表，请每条消息只发送一个哔哩哔哩链接。"
        bilibili_url = bilibili_urls[0]
        if bilibili_kind:
            return queue_download(
                source,
                chat_id,
                message_id,
                [bilibili_url],
                "audio" if bilibili_kind == "bilibili_audio" else "video",
                bilibili_kind,
            )
        if source != "telegram":
            return "哔哩哔哩链接需要选择类型；请使用 /biliaudio 链接 或 /bilivideo 链接。"
        try:
            send_bilibili_prompt(chat_id, actor_id, bilibili_url)
        except BilibiliIngestError as exc:
            return f"哔哩哔哩链接读取失败：{exc}"
        return None
    if bilibili_kind:
        return "命令后面需要附上哔哩哔哩视频链接。"

    mode = parse_mode(text)
    if apple_urls:
        return queue_download(source, chat_id, message_id, apple_urls, mode)

    query = search_query_from_text(text)
    if not query:
        return None
    if source != "telegram":
        return "文字搜索选择目前只支持 Telegram；Synology Chat 请继续发送 Apple Music 链接。"
    if not SEARCH_ENABLED:
        return "Apple Music 文字搜索未启用，请直接发送 Apple Music 链接。"
    if UNIFIED_MUSIC_ENABLED and mode == "alac":
        try:
            results, errors = unified_music_search(query)
        except Exception as exc:
            return f"统一搜索暂时不可用：{redact_sensitive_text(exc)}"
        if not results:
            return "没有找到可展示的歌曲。" + ("部分来源暂时不可用，请稍后重试。" if errors else "可以换个关键词。")
        send_search_results(chat_id, message_id, query, mode, results, actor_id)
        if errors:
            send_message(chat_id, "部分来源搜索暂时不可用；选择后仍会重新核对 Apple，再按 Apple → LX → SQ 尝试无损。")
        return None
    try:
        results = apple_music_search(query)
    except Exception as exc:
        error_text = redact_sensitive_text(exc)
        log(f"Apple Music search failed query={query!r}: {error_text}")
        return f"Apple Music 搜索失败：{error_text}"
    if not results:
        if LX_FALLBACK_ENABLED:
            send_auto_lx_search_results(
                chat_id,
                message_id,
                query,
                mode,
                actor_id,
            )
            return None
        return f"没有在 Apple Music 搜到“{query}”。可以换个关键词，或直接发送 Apple Music 链接。"
    send_search_results(chat_id, message_id, query, mode, results, actor_id)
    return None

def handle_youtube_callback(callback_query: dict[str, Any]) -> None:
    callback_id = str(callback_query.get("id") or "")
    data = str(callback_query.get("data") or "")
    parts = data.split(":")
    action = parts[1] if len(parts) >= 2 else ""
    key = parts[2] if len(parts) >= 3 else ""
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    actor = callback_query.get("from") or {}
    actor_id = str(actor.get("id") or "")
    if not chat_allowed(chat_id):
        answer_callback_query(callback_id, "这个聊天没有权限使用下载机器人。", show_alert=True)
        return
    choice = get_youtube_choice(key)
    if not choice or choice.chat_id != chat_id:
        answer_callback_query(callback_id, "这条 YouTube 选择已经过期，请重新发送链接。", show_alert=True)
        return
    if choice.actor_id and actor_id and choice.actor_id != actor_id:
        answer_callback_query(callback_id, "请由发送链接的人选择下载类型。", show_alert=True)
        return

    current_message_id = str(message.get("message_id") or "")
    if action == "cancel":
        pop_youtube_choice(key)
        edit_message_text(
            chat_id,
            current_message_id,
            f"已取消 YouTube 下载：{truncate_display_text(choice.title, 100)}",
            {"inline_keyboard": []},
        )
        answer_callback_query(callback_id, "已取消。")
        return
    if action not in {"audio", "video"}:
        answer_callback_query(callback_id, "未知的下载类型。", show_alert=True)
        return
    if choice.truncated or choice.item_count > YOUTUBE_PLAYLIST_MAX_ITEMS:
        answer_callback_query(
            callback_id,
            f"超过安全上限 {YOUTUBE_PLAYLIST_MAX_ITEMS} 项。",
            show_alert=True,
        )
        return

    pop_youtube_choice(key)
    kind = "youtube_audio" if action == "audio" else "youtube_video"
    mode = "audio" if action == "audio" else "video"
    message_id = f"yt{message.get('message_id', '0')}-{key}"
    queue_download(
        "telegram",
        chat_id,
        message_id,
        [choice.url],
        mode,
        kind,
        title=choice.title,
    )
    destination = "Navidrome" if action == "audio" else "Emby"
    edit_message_text(
        chat_id,
        current_message_id,
        f"已选择：{truncate_display_text(choice.title, 100)}\n"
        f"类型：{'音乐' if action == 'audio' else '视频'} -> {destination}\n"
        "任务卡片已创建。",
        {"inline_keyboard": []},
    )
    answer_callback_query(callback_id, "已加入下载队列。")


def handle_bilibili_callback(callback_query: dict[str, Any]) -> None:
    callback_id = str(callback_query.get("id") or "")
    data = str(callback_query.get("data") or "")
    parts = data.split(":")
    action = parts[1] if len(parts) >= 2 else ""
    key = parts[2] if len(parts) >= 3 else ""
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    actor = callback_query.get("from") or {}
    actor_id = str(actor.get("id") or "")
    if not chat_allowed(chat_id):
        answer_callback_query(callback_id, "这个聊天没有权限使用下载机器人。", show_alert=True)
        return
    choice = get_bilibili_choice(key)
    if not choice or choice.chat_id != chat_id:
        answer_callback_query(callback_id, "这条哔哩哔哩选择已经过期，请重新发送链接。", show_alert=True)
        return
    if choice.actor_id and actor_id and choice.actor_id != actor_id:
        answer_callback_query(callback_id, "请由发送链接的人选择下载类型。", show_alert=True)
        return

    current_message_id = str(message.get("message_id") or "")
    if action == "cancel":
        pop_bilibili_choice(key)
        edit_message_text(
            chat_id,
            current_message_id,
            f"已取消哔哩哔哩下载：{truncate_display_text(choice.title, 100)}",
            {"inline_keyboard": []},
        )
        answer_callback_query(callback_id, "已取消。")
        return
    if action not in {"audio", "video"}:
        answer_callback_query(callback_id, "未知的下载类型。", show_alert=True)
        return
    if choice.truncated or choice.item_count > BILIBILI_PLAYLIST_MAX_ITEMS:
        answer_callback_query(
            callback_id,
            f"超过安全上限 {BILIBILI_PLAYLIST_MAX_ITEMS} 项。",
            show_alert=True,
        )
        return

    pop_bilibili_choice(key)
    kind = "bilibili_audio" if action == "audio" else "bilibili_video"
    mode = "audio" if action == "audio" else "video"
    message_id = f"bili{message.get('message_id', '0')}-{key}"
    queue_download(
        "telegram",
        chat_id,
        message_id,
        [choice.url],
        mode,
        kind,
        title=choice.title,
    )
    destination = "Navidrome" if action == "audio" else "Emby"
    edit_message_text(
        chat_id,
        current_message_id,
        f"已选择：{truncate_display_text(choice.title, 100)}\n"
        f"类型：{'音乐' if action == 'audio' else '视频'} -> {destination}\n"
        "任务卡片已创建。",
        {"inline_keyboard": []},
    )
    answer_callback_query(callback_id, "已加入下载队列。")


def handle_video_delete_callback(callback_query: dict[str, Any]) -> None:
    callback_id = str(callback_query.get("id") or "")
    data = str(callback_query.get("data") or "")
    parts = data.split(":")
    action = parts[1] if len(parts) >= 2 else ""
    key = parts[2] if len(parts) >= 3 else ""
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    actor = callback_query.get("from") or {}
    actor_id = str(actor.get("id") or "")
    current_message_id = str(message.get("message_id") or "")
    if not chat_allowed(chat_id):
        answer_callback_query(
            callback_id,
            "这个聊天没有权限使用下载机器人。",
            show_alert=True,
        )
        return
    session = get_video_delete_session(key)
    if not session or session.chat_id != chat_id:
        answer_callback_query(
            callback_id,
            "这个删除菜单已经过期，请重新发送 /deletevideo。",
            show_alert=True,
        )
        return
    if session.actor_id and actor_id and session.actor_id != actor_id:
        answer_callback_query(
            callback_id,
            "请由打开删除菜单的人执行操作。",
            show_alert=True,
        )
        return

    if action == "cancel":
        invalidate_video_delete_session(key)
        edit_message_text(
            chat_id,
            current_message_id,
            "已关闭在线视频删除菜单。",
            {"inline_keyboard": []},
        )
        answer_callback_query(callback_id, "已关闭。")
        return

    if action == "noop":
        answer_callback_query(
            callback_id,
            f"共 {video_delete_page_count(session.entries)} 页",
        )
        return

    if action == "page" and len(parts) >= 4:
        try:
            requested_page = int(parts[3])
        except ValueError:
            requested_page = 0
        text, reply_markup, page = build_video_delete_page(
            session,
            key,
            requested_page,
        )
        edit_message_text(chat_id, current_message_id, text, reply_markup)
        answer_callback_query(
            callback_id,
            f"第 {page + 1}/{video_delete_page_count(session.entries)} 页",
        )
        return

    if action not in {"pick", "confirm"} or len(parts) < 4:
        answer_callback_query(callback_id, "未知的删除操作。", show_alert=True)
        return
    try:
        index = int(parts[3])
    except ValueError:
        index = -1
    if index < 0 or index >= len(session.entries):
        answer_callback_query(
            callback_id,
            "这个视频已经不在列表中，请重新打开删除菜单。",
            show_alert=True,
        )
        return
    selected = session.entries[index]
    current = load_video_delete_entry(selected)
    if not current:
        answer_callback_query(
            callback_id,
            "下载记录已不存在，请重新打开删除菜单。",
            show_alert=True,
        )
        return

    page = index // VIDEO_DELETE_PAGE_SIZE
    if action == "pick":
        try:
            directory = video_delete_directory(current)
        except VideoDeleteError as exc:
            answer_callback_query(callback_id, str(exc), show_alert=True)
            return
        size = media_path_size(str(directory))
        label = video_platform_config(current.platform)[0]
        downloaded = time.strftime(
            "%Y-%m-%d %H:%M",
            time.localtime(current.downloaded_at),
        )
        text = "\n".join(
            [
                "确认删除在线视频",
                f"平台：{label}",
                f"标题：{truncate_display_text(current.title, 120)}",
                f"下载时间：{downloaded}",
                f"目录体积：{format_file_size(size)}",
                f"目录：{video_delete_display_path(current, directory)}",
                "",
                "确认后会删除媒体、封面、NFO、字幕及下载记录，并刷新 Emby。",
            ]
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "确认删除",
                        "callback_data": (
                            f"{VIDEO_DELETE_CALLBACK_PREFIX}:confirm:{key}:{index}"
                        ),
                    }
                ],
                [
                    {
                        "text": "返回列表",
                        "callback_data": (
                            f"{VIDEO_DELETE_CALLBACK_PREFIX}:page:{key}:{page}"
                        ),
                    },
                    {
                        "text": "取消",
                        "callback_data": (
                            f"{VIDEO_DELETE_CALLBACK_PREFIX}:cancel:{key}"
                        ),
                    },
                ],
            ]
        }
        edit_message_text(
            chat_id,
            current_message_id,
            text,
            reply_markup,
        )
        answer_callback_query(callback_id, "请再次确认。")
        return

    if not online_video_library_lock.acquire(blocking=False):
        answer_callback_query(
            callback_id,
            "当前有在线视频正在入库，请稍后再确认删除。",
            show_alert=True,
        )
        return
    answer_callback_query(callback_id, "正在删除并刷新 Emby...")
    try:
        entry, directory, size, existed, scan_triggered = delete_video_entry(current)
    except Exception as exc:
        log(
            f"video delete failed platform={current.platform} "
            f"id={current.media_id}: {exc}"
        )
        text = (
            "删除失败，文件未完整清理。\n"
            f"原因：{truncate_display_text(str(exc), 500)}"
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "返回列表",
                        "callback_data": (
                            f"{VIDEO_DELETE_CALLBACK_PREFIX}:page:{key}:{page}"
                        ),
                    },
                    {
                        "text": "关闭",
                        "callback_data": (
                            f"{VIDEO_DELETE_CALLBACK_PREFIX}:cancel:{key}"
                        ),
                    },
                ]
            ]
        }
        edit_message_text(
            chat_id,
            current_message_id,
            text,
            reply_markup,
        )
        return
    finally:
        online_video_library_lock.release()

    invalidate_video_delete_session(key)
    label = video_platform_config(entry.platform)[0]
    result_text = "文件和下载记录已删除" if existed else "文件已不存在，下载记录已清理"
    edit_message_text(
        chat_id,
        current_message_id,
        "\n".join(
            [
                "在线视频删除完成",
                f"平台：{label}",
                f"标题：{truncate_display_text(entry.title, 120)}",
                f"释放空间：{format_file_size(size)}",
                f"目录：{video_delete_display_path(entry, directory)}",
                f"结果：{result_text}",
                (
                    "Emby 扫描：已触发"
                    if scan_triggered
                    else "Emby 扫描：未触发，将由定时扫描发现"
                ),
                "该源链接以后仍可重新下载。",
            ]
        ),
        {"inline_keyboard": []},
    )


def handle_word_lyrics_mac_callback(callback_query: dict[str, Any]) -> None:
    callback_id = str(callback_query.get("id") or "")
    data = str(callback_query.get("data") or "")
    parts = data.split(":")
    action = parts[1] if len(parts) == 3 else ""
    try:
        job_id = int(parts[2]) if len(parts) == 3 else 0
    except ValueError:
        job_id = 0
    if action not in {"approve", "decline"} or job_id <= 0:
        answer_callback_query(callback_id, "无效的逐词歌词操作。", show_alert=True)
        return

    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        actor = callback_query.get("from") or {}
        chat_id = str(actor.get("id") or "")
    if not chat_allowed(chat_id):
        answer_callback_query(callback_id, "这个聊天没有权限操作任务。", show_alert=True)
        return

    database_path = STATE_DIR / "word-lyrics" / "automation" / "queue.sqlite3"
    store = QueueStore(database_path)
    job = store.get(job_id)
    if job is None:
        answer_callback_query(callback_id, "逐词歌词任务不存在。", show_alert=True)
        return
    context = job.metadata.get("notification")
    expected_chat_id = (
        str(context.get("chat_id") or "") if isinstance(context, dict) else ""
    )
    if not expected_chat_id or expected_chat_id != chat_id:
        answer_callback_query(callback_id, "这张卡片不属于当前聊天。", show_alert=True)
        return

    approved = action == "approve"
    try:
        updated = store.set_mac_consent(
            job_id,
            approved=approved,
            actor=f"telegram:{chat_id}",
        )
    except RuntimeError as exc:
        answer_callback_query(
            callback_id,
            truncate_display_text(str(exc), 160),
            show_alert=True,
        )
        return

    title = truncate_display_text(str(job.metadata.get("title") or "未知标题"), 120)
    artist = truncate_display_text(str(job.metadata.get("artist") or "未知艺人"), 120)
    if approved:
        text = "\n".join(
            [
                "已授权 Mac 高精度重试 ✅",
                f"标题：{title}",
                f"艺人：{artist}",
                "任务已进入仅限 Mac 的授权队列。",
                "只有这个经过你确认的任务可以把音频传给 Mac；其他任务不会调用 Mac。",
                "Mac 在线时通常会在 3 分钟内自动领取；无需打开 Codex。",
                "高精度模型已保存在 Mac，本次不会重复下载模型。",
                "无词录音经核验后结案；暂缺来源会通知等待原因和下次重查时间，随后每六小时重查；最终结果另行通知。",
                f"后台任务：#{updated.id}",
            ]
        )
        callback_text = "已记录本次 Mac 授权。"
    else:
        text = "\n".join(
            [
                "已停止本次逐词歌词升级",
                f"标题：{title}",
                f"艺人：{artist}",
                "按你的选择不调用 Mac；原有歌词和音频均保持不变。",
                f"后台任务：#{updated.id}",
            ]
        )
        callback_text = "已保留现状，不会调用 Mac。"
    message_id = str(message.get("message_id") or "")
    if message_id:
        edit_message_text(
            chat_id,
            message_id,
            text,
            {"inline_keyboard": []},
        )
    answer_callback_query(callback_id, callback_text)


def handle_callback_query(callback_query: dict[str, Any]) -> None:
    callback_id = str(callback_query.get("id") or "")
    data = str(callback_query.get("data") or "")
    if data.startswith("task:"):
        handle_task_card_callback(callback_query)
        return
    if data.startswith(f"{VIDEO_DELETE_CALLBACK_PREFIX}:"):
        handle_video_delete_callback(callback_query)
        return
    if data.startswith(f"{YOUTUBE_CALLBACK_PREFIX}:"):
        handle_youtube_callback(callback_query)
        return
    if data.startswith(f"{BILIBILI_CALLBACK_PREFIX}:"):
        handle_bilibili_callback(callback_query)
        return
    if data.startswith(f"{WORD_LYRICS_MAC_CALLBACK_PREFIX}:"):
        handle_word_lyrics_mac_callback(callback_query)
        return
    if not data.startswith(f"{SEARCH_CALLBACK_PREFIX}:"):
        log(f"ignored telegram callback data={data[:32]!r}")
        return
    log(f"telegram callback received data={data[:48]!r}")
    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        from_user = callback_query.get("from") or {}
        chat_id = str(from_user.get("id") or "")
    if not chat_allowed(chat_id):
        log(f"ignored unauthorized callback chat_id={chat_id}")
        answer_callback_query(callback_id, "这个聊天没有权限使用下载机器人。", show_alert=True)
        return

    parts = data.split(":")
    action = parts[1] if len(parts) >= 2 else ""
    if len(parts) >= 3:
        owned_session = get_search_session(parts[2])
        actor_id = str((callback_query.get("from") or {}).get("id") or "")
        if owned_session and (owned_session.chat_id != chat_id or owned_session.scope_key not in {chat_id, search_scope_key(chat_id, actor_id)}):
            answer_callback_query(callback_id, "请使用自己发起的搜索结果。", show_alert=True)
            return
    if action == "lx" and len(parts) >= 3:
        session_key = parts[2]
        session = get_search_session(session_key)
        if not session:
            answer_callback_query(callback_id, "这个搜索结果已过期，请重新搜索。", show_alert=True)
            return
        if not is_active_search_session(session_key, session):
            close_search_message(chat_id, message, search_session_stale_text(session))
            answer_callback_query(
                callback_id,
                "这条搜索已被新的搜索替代，请使用最新一条结果。",
                show_alert=True,
            )
            return
        if not LX_FALLBACK_ENABLED:
            answer_callback_query(callback_id, "LX 搜索当前未启用。", show_alert=True)
            return
        try:
            apple_page = int(parts[3]) if len(parts) >= 4 else 0
        except ValueError:
            apple_page = 0
        apple_page = clamp_search_page(session.results, apple_page)
        answer_callback_query(callback_id, "正在转到 LX 高品质搜索……")
        loading_text, loading_keyboard = build_lx_search_loading_page(
            session.query,
            session_key,
            has_apple_results=bool(session.results),
        )
        edit_search_message(
            chat_id,
            message,
            loading_text,
            loading_keyboard,
        )
        lx_session = get_lx_search_session(session_key)
        if lx_session is None:
            try:
                lx_results = query_lx_flac_results(session.query)
                lx_session = cache_lx_search_session(
                    session_key,
                    session.query,
                    apple_page,
                    lx_results,
                )
            except Exception as exc:
                error_text = redact_sensitive_text(exc)
                log(f"LX search failed query={session.query!r}: {error_text}")
                failure_text = "\n".join(
                    [
                        f"<b>LX Music 高品质搜索：{html_escape_text(truncate_display_text(session.query, 32))}</b>",
                        "LX 搜索暂时不可用，请稍后重试或返回 Apple 搜索。",
                    ]
                )
                edit_search_message(
                    chat_id,
                    message,
                    failure_text,
                    {
                        "inline_keyboard": lx_search_navigation_keyboard(
                            session_key,
                            has_apple_results=bool(session.results),
                        )
                    },
                )
                return
        text, reply_markup = build_lx_search_results_page(
            lx_session.query,
            lx_session.results,
            0,
            session_key,
            has_apple_results=bool(session.results),
        )
        edit_search_message(chat_id, message, text, reply_markup)
        append_journal(
            {
                "event": "lx_search_opened",
                "chat_id": chat_id,
                "query": session.query,
                "result_count": len(lx_session.results),
                "time": time.time(),
            }
        )
        return

    if action == "lxpage" and len(parts) >= 4:
        session_key = parts[2]
        session = get_search_session(session_key)
        lx_session = get_lx_search_session(session_key)
        if not session or not lx_session:
            answer_callback_query(callback_id, "这个 LX 搜索结果已过期，请重新搜索。", show_alert=True)
            return
        if not is_active_search_session(session_key, session):
            close_search_message(chat_id, message, search_session_stale_text(session))
            answer_callback_query(
                callback_id,
                "这条搜索已被新的搜索替代，请使用最新一条结果。",
                show_alert=True,
            )
            return
        try:
            page = int(parts[3])
        except ValueError:
            page = 0
        page = clamp_lx_search_page(lx_session.results, page)
        text, reply_markup = build_lx_search_results_page(
            lx_session.query,
            lx_session.results,
            page,
            session_key,
            has_apple_results=bool(session.results),
        )
        edit_search_message(chat_id, message, text, reply_markup)
        answer_callback_query(
            callback_id,
            f"LX 第 {page + 1}/{lx_search_page_count(lx_session.results)} 页",
        )
        return

    if action == "lxnoop" and len(parts) >= 3:
        session_key = parts[2]
        session = get_search_session(session_key)
        lx_session = get_lx_search_session(session_key)
        if session and lx_session and is_active_search_session(session_key, session):
            answer_callback_query(
                callback_id,
                f"LX 共 {lx_search_page_count(lx_session.results)} 页",
            )
        else:
            answer_callback_query(callback_id, "这个 LX 搜索结果已过期。", show_alert=True)
        return

    if action == "lxback" and len(parts) >= 3:
        session_key = parts[2]
        session = get_search_session(session_key)
        if not session:
            answer_callback_query(callback_id, "这个搜索结果已过期，请重新搜索。", show_alert=True)
            return
        if not is_active_search_session(session_key, session):
            close_search_message(chat_id, message, search_session_stale_text(session))
            answer_callback_query(
                callback_id,
                "这条搜索已被新的搜索替代，请使用最新一条结果。",
                show_alert=True,
            )
            return
        if not session.results:
            answer_callback_query(
                callback_id,
                "Apple Music 没有可返回的结果。",
                show_alert=True,
            )
            return
        lx_session = get_lx_search_session(session_key)
        apple_page = lx_session.apple_page if lx_session else 0
        text, reply_markup, artwork_url = build_search_results_page(
            session.query,
            session.mode,
            session.results,
            apple_page,
            session_key,
        )
        edit_search_message(
            chat_id,
            message,
            text,
            reply_markup,
            artwork_url=artwork_url,
        )
        answer_callback_query(callback_id, "已返回 Apple 搜索。")
        return

    if action == "lxsel" and len(parts) >= 4:
        session_key = parts[2]
        session = get_search_session(session_key)
        lx_session = get_lx_search_session(session_key)
        if not session or not lx_session:
            answer_callback_query(callback_id, "这个 LX 搜索结果已过期，请重新搜索。", show_alert=True)
            return
        if not is_active_search_session(session_key, session):
            close_search_message(chat_id, message, search_session_stale_text(session))
            answer_callback_query(
                callback_id,
                "这条搜索已被新的搜索替代，请使用最新一条结果。",
                show_alert=True,
            )
            return
        try:
            index = int(parts[3])
        except ValueError:
            index = 0
        if index < 1 or index > len(lx_session.results):
            answer_callback_query(callback_id, "这个 LX 候选项不存在。", show_alert=True)
            return
        result = lx_session.results[index - 1]
        invalidate_search_session(session_key)
        message_id = f"lxcb{message.get('message_id', '0')}-{session_key}-{index}"
        queue_download(
            "telegram",
            chat_id,
            message_id,
            [
                "lx://"
                f"{urllib.parse.quote(result.source, safe='')}/"
                f"{urllib.parse.quote(result.songmid, safe='')}"
            ],
            LX_FALLBACK_QUALITY,
            kind="lx_music",
            title=f"{result.artist} - {result.title}",
            payload={
                "song_info": result.song_info,
                "source": result.source,
                "songmid": result.songmid,
                "title": result.title,
                "artist": result.artist,
                "album": result.album,
            },
        )
        append_journal(
            {
                "event": "lx_search_selected",
                "chat_id": chat_id,
                "message_id": message_id,
                "query": session.query,
                "index": index,
                "result": {
                    "title": result.title,
                    "artist": result.artist,
                    "album": result.album,
                    "source": result.source,
                    "songmid": result.songmid,
                    "quality": result.quality,
                },
                "time": time.time(),
            }
        )
        quality_label = lx_quality_label(result.quality)
        answer_callback_query(callback_id, f"已加入 LX {quality_label} 下载队列。")
        close_search_message(
            chat_id,
            message,
            f"已选择 LX {html_escape_text(quality_label)}："
            f"<b>{html_escape_text(truncate_display_text(result.title, 40))}</b>\n"
            f"{html_escape_text(truncate_display_text(result.artist, 40))} · "
            f"{html_escape_text(lx_source_label(result.source))}\n"
            "这条搜索已关闭，下载进度请看新的任务卡。",
        )
        return

    if action == "cancel" and len(parts) >= 3:
        session_key = parts[2]
        session = invalidate_search_session(session_key)
        if not session:
            answer_callback_query(callback_id, "这条搜索已经失效。", show_alert=True)
            return
        close_search_message(
            chat_id,
            message,
            "已取消本次搜索："
            f"<b>{html_escape_text(truncate_display_text(session.query, 40))}</b>",
        )
        answer_callback_query(callback_id, "已取消本次搜索。")
        return

    if action == "page" and len(parts) >= 4:
        session_key = parts[2]
        session = get_search_session(session_key)
        if not session:
            log(f"telegram page callback cache miss key={session_key}")
            answer_callback_query(callback_id, "这个搜索结果已过期，请重新搜索。", show_alert=True)
            return
        if not is_active_search_session(session_key, session):
            log(f"telegram page callback inactive key={session_key}")
            close_search_message(chat_id, message, search_session_stale_text(session))
            answer_callback_query(
                callback_id,
                "这条搜索已被新的搜索替代，请使用最新一条结果。",
                show_alert=True,
            )
            return
        try:
            page = int(parts[3])
        except ValueError:
            page = 0
        page = clamp_search_page(session.results, page)
        text, reply_markup, artwork_url = build_search_results_page(
            session.query, session.mode, session.results, page, session_key
        )
        current_message_id = str(message.get("message_id") or "")
        edited = False
        if message.get("photo"):
            if artwork_url:
                edited = edit_message_photo(
                    chat_id, current_message_id, artwork_url, text, reply_markup, "HTML"
                )
            if not edited:
                edited = edit_message_caption(
                    chat_id, current_message_id, text, reply_markup, "HTML"
                )
        if not edited:
            edit_message_text(chat_id, current_message_id, text, reply_markup, "HTML")
        answer_callback_query(callback_id, f"第 {page + 1}/{search_page_count(session.results)} 页")
        return

    if action == "noop":
        session_key = parts[2] if len(parts) >= 3 else ""
        session = get_search_session(session_key) if session_key else None
        if session and is_active_search_session(session_key, session):
            answer_callback_query(callback_id, f"共 {search_page_count(session.results)} 页")
        elif session:
            close_search_message(chat_id, message, search_session_stale_text(session))
            answer_callback_query(
                callback_id,
                "这条搜索已被新的搜索替代，请使用最新一条结果。",
                show_alert=True,
            )
        else:
            answer_callback_query(callback_id, "这个搜索结果已过期，请重新搜索。", show_alert=True)
        return

    if action == "sel" and len(parts) >= 4:
        session_key = parts[2]
        session = get_search_session(session_key)
        if not session:
            log(f"telegram selection callback cache miss key={session_key}")
            answer_callback_query(callback_id, "这个搜索结果已过期，请重新搜索。", show_alert=True)
            return
        if not is_active_search_session(session_key, session):
            log(f"telegram selection callback inactive key={session_key}")
            close_search_message(chat_id, message, search_session_stale_text(session))
            answer_callback_query(
                callback_id,
                "这条搜索已被新的搜索替代，请使用最新一条结果。",
                show_alert=True,
            )
            return
        try:
            index = int(parts[3])
        except ValueError:
            index = 0
        if index < 1 or index > len(session.results):
            answer_callback_query(callback_id, "这个候选项不存在，请重新搜索。", show_alert=True)
            return
        result = session.results[index - 1]
        mode = session.mode
        invalidate_search_session(session_key)
        message_id = f"cb{message.get('message_id', '0')}-{session_key}-{index}"
        queue_download(
            "telegram",
            chat_id,
            message_id,
            [result.url],
            mode,
            title=f"{result.artist} - {result.title}",
            payload={"unified_music": result.unified_payload} if result.unified_payload else None,
        )
        append_journal(
            {
                "event": "search_selected",
                "chat_id": chat_id,
                "message_id": message_id,
                "query": session.query,
                "index": index,
                "mode": mode,
                "result": {
                    "title": result.title,
                    "artist": result.artist,
                    "album": result.album,
                    "url": result.url,
                    "track_id": result.track_id,
                },
                "time": time.time(),
            }
        )
        answer_callback_query(callback_id, "已加入下载队列。")
        close_search_message(
            chat_id,
            message,
            f"已选择：<b>{html_escape_text(truncate_display_text(result.title, 40))}</b>\n"
            f"{html_escape_text(truncate_display_text(result.artist, 40))}\n"
            "这条搜索已关闭，避免重复选择。",
        )
        return

    key = data.split(":", 1)[1]
    cached = pop_search_result(key)
    if not cached:
        log(f"telegram callback cache miss key={key}")
        answer_callback_query(callback_id, "这个搜索结果已过期，请重新搜索。", show_alert=True)
        return
    mode, result = cached
    message_id = f"cb{message.get('message_id', '0')}-{key}"
    queue_download(
        "telegram",
        chat_id,
        message_id,
        [result.url],
        mode,
        title=f"{result.artist} - {result.title}",
    )
    append_journal(
        {
            "event": "search_selected",
            "chat_id": chat_id,
            "message_id": message_id,
            "mode": mode,
            "result": {
                "title": result.title,
                "artist": result.artist,
                "album": result.album,
                "url": result.url,
                "track_id": result.track_id,
            },
            "time": time.time(),
        }
    )
    answer_callback_query(callback_id, "已加入下载队列。")


def handle_update(update: dict[str, Any]) -> None:
    callback_query = update.get("callback_query")
    if isinstance(callback_query, dict):
        handle_callback_query(callback_query)
        return

    parsed = get_message_text(update)
    if not parsed:
        log(f"ignored telegram update keys={sorted(update.keys())}")
        return
    chat_id, text, message_id, sender_id = parsed
    if not chat_id:
        return
    if not chat_allowed(chat_id):
        log(f"ignored unauthorized chat_id={chat_id}")
        return

    urls = [*extract_urls(text), *extract_youtube_urls(text), *extract_bilibili_urls(text)]
    preview = re.sub(r"\s+", " ", text).strip()[:160]
    message = telegram_message_from_update(update) or {}
    try:
        delivery_lag = max(0, int(time.time()) - int(message.get("date", 0)))
    except (TypeError, ValueError):
        delivery_lag = 0
    log(
        f"telegram update chat_id={chat_id} message_id={message_id} "
        f"delivery_lag={delivery_lag}s text_len={len(text)} "
        f"urls={len(urls)} preview={preview!r}"
    )
    response = handle_text("telegram", chat_id, text, str(message_id), sender_id)
    if response:
        send_message(chat_id, response)


class SynologyChatHandler(http.server.BaseHTTPRequestHandler):
    server_version = "AppleMusicIngest/1.0"

    def log_message(self, format_string: str, *args: Any) -> None:
        log(f"synology-chat {self.address_string()} {format_string % args}")

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in {"/health", SYN_CHAT_WEBHOOK_PATH}:
            notification_age = max(
                0.0,
                time.monotonic() - word_lyrics_notification_heartbeat,
            )
            notification_ok = not TOKEN or notification_age <= 300
            self.send_json(
                200 if notification_ok else 503,
                {
                    "ok": notification_ok,
                    "queue": job_queue.qsize(),
                    "wordLyricsNotificationAgeSeconds": round(notification_age, 3),
                },
            )
        else:
            self.send_json(404, {"text": "not found"})


    def handle_local_media_task(self) -> None:
        remote_address = str(self.client_address[0] or "")
        local_network = (
            remote_address in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
            or remote_address.startswith(("10.", "192.168."))
            or bool(re.fullmatch(r"172\.(?:1[6-9]|2\d|3[01])\..+", remote_address))
        )
        if not local_network:
            self.send_json(403, {"ok": False, "error": "loopback_only"})
            return

        try:
            length = int(self.headers.get("content-length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 32 * 1024:
            self.send_json(413 if length > 32 * 1024 else 400, {"ok": False, "error": "invalid_body_size"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"ok": False, "error": "invalid_json"})
            return
        if not isinstance(payload, dict):
            self.send_json(400, {"ok": False, "error": "invalid_media_task"})
            return

        platform = str(payload.get("platform") or "").strip().lower()
        mode = str(payload.get("mode") or "").strip().lower()
        request_id = str(payload.get("requestId") or "").strip()
        user_id = str(payload.get("userId") or "").strip()
        submitted_url = str(payload.get("url") or "").strip()
        title = str(payload.get("title") or "").strip()[:240]
        dry_run = payload.get("dryRun") is True
        if mode not in {"audio", "video"}:
            self.send_json(400, {"ok": False, "error": "invalid_mode"})
            return
        if not re.fullmatch(r"[0-9A-Za-z_.:-]{1,128}", request_id):
            self.send_json(400, {"ok": False, "error": "invalid_request_id"})
            return
        if not re.fullmatch(r"\d{1,24}", user_id):
            self.send_json(400, {"ok": False, "error": "invalid_user_id"})
            return

        if platform == "youtube":
            if not YOUTUBE_ENABLED:
                self.send_json(409, {"ok": False, "error": "youtube_disabled"})
                return
            urls = extract_youtube_urls(submitted_url)
            kind = "youtube_audio" if mode == "audio" else "youtube_video"
        elif platform == "bilibili":
            if not BILIBILI_ENABLED:
                self.send_json(409, {"ok": False, "error": "bilibili_disabled"})
                return
            urls = extract_bilibili_urls(submitted_url)
            kind = "bilibili_audio" if mode == "audio" else "bilibili_video"
        else:
            self.send_json(400, {"ok": False, "error": "invalid_platform"})
            return
        if len(urls) != 1:
            self.send_json(400, {"ok": False, "error": "invalid_media_url"})
            return

        if dry_run:
            self.send_json(
                200,
                {
                    "ok": True,
                    "accepted": False,
                    "duplicate": False,
                    "dryRun": True,
                    "task": f"validated {platform}/{mode}",
                },
            )
            return

        response = queue_download(
            "n8n",
            user_id,
            request_id,
            [urls[0]],
            mode,
            kind,
            title=title,
        )
        duplicate = bool(response and response.startswith("任务已存在"))
        self.send_json(
            202,
            {
                "ok": True,
                "accepted": True,
                "duplicate": duplicate,
                "task": (response or "任务已进入下载队列。")[:1000],
            },
        )

    def do_POST(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path == LOCAL_MEDIA_TASK_PATH:
            self.handle_local_media_task()
            return
        if parsed_url.path != SYN_CHAT_WEBHOOK_PATH:
            self.send_json(404, {"text": "not found"})
            return

        length = min(int(self.headers.get("content-length", "0") or "0"), 1024 * 1024)
        raw_body = self.rfile.read(length)
        data = parse_synology_body(self.headers.get("content-type", ""), raw_body)
        query = urllib.parse.parse_qs(parsed_url.query)

        token = (
            first_text(data, ("token", "outgoing_token", "payload.token"))
            or first_text({key: values for key, values in query.items()}, ("token",))
            or self.headers.get("x-synology-token", "").strip()
            or self.headers.get("x-webhook-token", "").strip()
        )
        auth_header = self.headers.get("authorization", "")
        if not token and auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

        if not SYN_CHAT_OUTGOING_TOKEN or not hmac.compare_digest(token, SYN_CHAT_OUTGOING_TOKEN):
            self.send_json(403, {"text": "Invalid token"})
            return

        user_id = first_text(data, ("user_id", "userid", "uid", "payload.user_id")) or "synology"
        if not synology_user_allowed(user_id):
            log(f"ignored unauthorized Synology Chat user_id={user_id}")
            self.send_json(403, {"text": "User not authorized"})
            return

        text = first_text(
            data,
            (
                "text",
                "message",
                "payload.text",
                "payload.message",
                "post_content",
                "payload.post_content",
                "content",
                "payload.content",
                "command",
            ),
        )
        if not text:
            self.send_json(400, {"text": "Missing text"})
            return

        message_id = first_text(data, ("message_id", "post_id", "payload.message_id"))
        if not message_id:
            message_id = str(int(time.time() * 1000))

        channel_id = first_text(data, ("channel_id", "payload.channel_id"))
        urls = [*extract_urls(text), *extract_youtube_urls(text), *extract_bilibili_urls(text)]
        log(
            f"synology update user_id={user_id} channel_id={channel_id or '-'} "
            f"message_id={message_id} text_len={len(text)} urls={len(urls)} "
            f"preview={text[:160]!r}"
        )
        response = handle_text("synology", user_id, text, message_id)
        if not response:
            response = ""
        self.send_json(200, {"text": response[:3900]})


def synology_server_loop() -> None:
    server = http.server.ThreadingHTTPServer((SYN_CHAT_BIND, SYN_CHAT_PORT), SynologyChatHandler)
    server.timeout = 2
    if SYN_CHAT_ENABLED and SYN_CHAT_OUTGOING_TOKEN:
        log(
            "Synology Chat webhook listening on "
            f"{SYN_CHAT_BIND}:{SYN_CHAT_PORT}{SYN_CHAT_WEBHOOK_PATH}"
        )
    else:
        log(f"health endpoint listening on {SYN_CHAT_BIND}:{SYN_CHAT_PORT}/health")
    while not stop_event.is_set():
        server.handle_request()
    server.server_close()


def register_telegram_commands() -> None:
    commands = [
        {"command": "start", "description": "查看下载机器人使用说明"},
        {"command": "search", "description": "统一搜索 Apple / LX / SQ" if UNIFIED_MUSIC_ENABLED else "搜索 Apple Music"},
        {"command": "alac", "description": "单曲无损：Apple → LX → SQ" if UNIFIED_MUSIC_ENABLED else "下载 Apple Music ALAC"},
        {"command": "atmos", "description": "下载 Apple Music Atmos"},
        {"command": "both", "description": "同时下载 ALAC 和 Atmos"},
    ]
    if YOUTUBE_ENABLED:
        commands.extend(
            [
                {"command": "ytmusic", "description": "YouTube 音乐入库 Navidrome"},
                {"command": "ytvideo", "description": "YouTube 视频入库 Emby"},
            ]
        )
    if BILIBILI_ENABLED:
        commands.extend(
            [
                {"command": "biliaudio", "description": "哔哩哔哩音乐入库 Navidrome"},
                {"command": "bilivideo", "description": "哔哩哔哩视频入库 Emby"},
            ]
        )
    if YOUTUBE_ENABLED or BILIBILI_ENABLED:
        commands.append(
            {"command": "deletevideo", "description": "分页选择并安全删除在线视频"}
        )
    commands.extend(
        [
            {"command": "status", "description": "查看当前任务和排队数量"},
            {"command": "progress", "description": "查看当前下载进度和速度"},
            {"command": "help", "description": "查看完整使用说明"},
        ]
    )
    try:
        telegram_api("setMyCommands", {"commands": commands}, timeout=30)
        log("Telegram command menu registered")
    except Exception as exc:
        log(f"failed to register Telegram command menu: {exc}")


def poll_loop() -> None:
    offset = load_offset()
    consecutive_errors = 0
    log(
        "Telegram polling configured "
        f"long_poll={TELEGRAM_POLL_TIMEOUT_SECONDS}s "
        f"http_timeout={TELEGRAM_POLL_REQUEST_TIMEOUT_SECONDS}s "
        f"idle={TELEGRAM_POLL_IDLE_SECONDS}s"
    )
    while not stop_event.is_set():
        try:
            payload: dict[str, Any] = {
                "timeout": TELEGRAM_POLL_TIMEOUT_SECONDS,
                "allowed_updates": [
                    "message",
                    "edited_message",
                    "channel_post",
                    "edited_channel_post",
                    "callback_query",
                ],
            }
            if offset is not None:
                payload["offset"] = offset
            updates = telegram_api(
                "getUpdates",
                payload,
                timeout=TELEGRAM_POLL_REQUEST_TIMEOUT_SECONDS,
            )
            consecutive_errors = 0
            if not updates and TELEGRAM_POLL_TIMEOUT_SECONDS == 0:
                time.sleep(TELEGRAM_POLL_IDLE_SECONDS)
            for update in updates:
                update_id = int(update["update_id"])
                handle_update(update)
                offset = update_id + 1
                save_offset(offset)
        except urllib.error.URLError as exc:
            consecutive_errors += 1
            log(f"telegram network error ({consecutive_errors} consecutive): {exc}")
            if (
                TELEGRAM_POLL_MAX_CONSECUTIVE_ERRORS
                and consecutive_errors >= TELEGRAM_POLL_MAX_CONSECUTIVE_ERRORS
            ):
                log("telegram poll stayed unhealthy; exiting for Docker restart")
                os._exit(75)
            time.sleep(TELEGRAM_POLL_ERROR_DELAY_SECONDS)
        except Exception as exc:
            consecutive_errors += 1
            log(f"poll error ({consecutive_errors} consecutive): {exc}")
            if (
                TELEGRAM_POLL_MAX_CONSECUTIVE_ERRORS
                and consecutive_errors >= TELEGRAM_POLL_MAX_CONSECUTIVE_ERRORS
            ):
                log("telegram poll stayed unhealthy; exiting for Docker restart")
                os._exit(75)
            time.sleep(TELEGRAM_POLL_ERROR_DELAY_SECONDS)


def handle_signal(signum: int, _frame: Any) -> None:
    log(f"received signal {signum}, stopping")
    stop_event.set()


def main() -> int:
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_PATH.mkdir(parents=True, exist_ok=True)

    chat_ready = SYN_CHAT_ENABLED and bool(SYN_CHAT_OUTGOING_TOKEN)
    if not TOKEN and not chat_ready:
        log("Telegram token and Synology Chat token are empty; container will idle until .env is updated")
        while not stop_event.is_set():
            time.sleep(60)
        return 0

    if TOKEN and not ALLOWED_CHAT_IDS:
        log("warning: TELEGRAM_ALLOWED_CHAT_IDS is empty; all chats can submit jobs")
    if TELEGRAM_PROXY_URL:
        log("Telegram proxy is enabled")
    if TELEGRAM_POLL_PROXY_URL != TELEGRAM_PROXY_URL:
        log("Telegram polling uses a dedicated proxy route")
    if YOUTUBE_ENABLED:
        log(
            "YouTube ingest enabled; "
            f"playlist_limit={YOUTUBE_PLAYLIST_MAX_ITEMS}; cookies={youtube_cookie_status()}"
        )
    if BILIBILI_ENABLED:
        log(
            "Bilibili ingest enabled; "
            f"playlist_limit={BILIBILI_PLAYLIST_MAX_ITEMS}; cookies={bilibili_cookie_status()}"
        )
    if SYN_CHAT_ENABLED and not SYN_CHAT_OUTGOING_TOKEN:
        log("warning: Synology Chat is enabled but outgoing token is empty; webhook disabled")
    if chat_ready and not SYN_CHAT_ALLOWED_USER_IDS:
        log("warning: SYNOLOGY_CHAT_ALLOWED_USER_IDS is empty; all valid-token Chat users can submit jobs")
    if chat_ready and not SYN_CHAT_INCOMING_WEBHOOK_URL:
        log("warning: Synology Chat incoming webhook URL is empty; only immediate webhook responses will be sent")
    if LX_FALLBACK_ENABLED:
        signatures = "66"
        if LX_FALLBACK_ALLOW_CATALOG_API_3076:
            signatures += ",67/3076"
        log(
            "LX high-quality fallback enabled; "
            f"eligible_signatures={signatures}; sources={','.join(LX_FALLBACK_SEARCH_SOURCES)}; "
            f"mode=alac; single-song-only=true; lossless-first=true; "
            f"allow-verified-320k={str(LX_FALLBACK_ALLOW_320).lower()}; "
            f"cross-source-attempts={LX_FALLBACK_MAX_CANDIDATES}; full-decode-verify=true"
        )

    start_wrapper_forwarders()

    if TOKEN:
        register_telegram_commands()

    recover_task_card_jobs()
    worker = threading.Thread(target=worker_loop, daemon=True)
    worker.start()
    card_thread = threading.Thread(target=task_card_loop, daemon=True)
    card_thread.start()

    word_lyrics_notification_thread: threading.Thread | None = None
    if TOKEN:
        word_lyrics_notification_thread = threading.Thread(
            target=word_lyrics_notification_loop,
            daemon=True,
        )
        word_lyrics_notification_thread.start()

    chat_thread: threading.Thread | None = None
    if chat_ready or HEALTH_SERVER_ENABLED:
        chat_thread = threading.Thread(target=synology_server_loop, daemon=True)
        chat_thread.start()

    if TOKEN:
        poll_loop()
    else:
        while not stop_event.is_set():
            time.sleep(1)

    worker.join(timeout=5)
    card_thread.join(timeout=5)
    if word_lyrics_notification_thread:
        word_lyrics_notification_thread.join(timeout=5)
    if chat_thread:
        chat_thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
