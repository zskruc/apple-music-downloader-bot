from __future__ import annotations

import hashlib
import html
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mutagen import File as MutagenFile
from mutagen.mp4 import MP4

from download_history import prepare_attempt_archive, commit_imported_media


YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
GENERIC_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,;:!?，。；：！？)]）】>\"'"
LRC_TIMESTAMP_RE = re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")
SRT_TIMING_RE = re.compile(
    r"^\s*(?P<start>\d{1,3}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d{1,3}:\d{2}:\d{2}[,.]\d{3})(?:\s+.*)?$"
)
CJK_TEXT_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
MEDIA_SUFFIXES = {".m4a", ".mp4", ".mkv", ".webm", ".opus", ".mp3"}
VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm"}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


ENABLED = env_flag("YT_ENABLED", True)
AUTH_AUTO_REFRESH_DISABLED = env_flag(
    "YT_AUTH_AUTO_REFRESH_DISABLED",
    False,
)
STATE_DIR = Path(env("YT_STATE_DIR", "/state/youtube"))
TEMP_ROOT = Path(env("YT_TEMP_PATH", "/state/youtube/tmp"))
AUDIO_ROOT = Path(env("YT_AUDIO_OUTPUT_PATH", "/youtube-music"))
VIDEO_ROOT = Path(env("YT_VIDEO_OUTPUT_PATH", "/youtube-video"))
COOKIE_PATH = Path(env("YT_COOKIES_PATH", "/state/youtube/cookies.txt"))
PROXY_URL = env("YT_DOWNLOAD_PROXY_URL", "http://host.docker.internal:7890")
LARGE_DOWNLOAD_PROXY_URL = env(
    "YT_LARGE_DOWNLOAD_PROXY_URL",
    "http://host.docker.internal:7891",
)
LARGE_DOWNLOAD_THRESHOLD_BYTES = max(
    0,
    int(env("YT_LARGE_DOWNLOAD_THRESHOLD_MIB", "500") or "500"),
) * 1024 * 1024
DOWNLOAD_SIZE_PROBE_TIMEOUT_SECONDS = max(
    30,
    int(env("YT_DOWNLOAD_SIZE_PROBE_TIMEOUT_SECONDS", "300") or "300"),
)
PO_TOKEN_ENABLED = env_flag("YT_PO_TOKEN_ENABLED", True)
PO_TOKEN_PROVIDER_URL = env(
    "YT_PO_TOKEN_PROVIDER_URL",
    "http://youtube-pot:4416",
).rstrip("/")
PO_TOKEN_CONTAINER_NAME = env(
    "YT_PO_TOKEN_CONTAINER_NAME",
    "youtube-pot-provider",
)
DOCKER_SOCKET_PATH = Path(env("DOCKER_SOCKET_PATH", "/var/run/docker.sock"))
PLAYER_CLIENT = env("YT_PLAYER_CLIENT", "mweb")
AUTH_RETRY_DELAY_SECONDS = max(
    0,
    int(env("YT_AUTH_RETRY_DELAY_SECONDS", "2") or "2"),
)
HIGH_QUALITY_RETRY_DELAY_SECONDS = max(
    15,
    int(env("YT_HIGH_QUALITY_RETRY_DELAY_SECONDS", "60") or "60"),
)
REQUIRED_COOKIE_NAMES = {
    name.strip()
    for name in env(
        "YT_REQUIRED_COOKIE_NAMES",
        "SID,HSID,SSID,APISID,SAPISID,LOGIN_INFO,__Secure-1PSID,__Secure-3PSID",
    ).split(",")
    if name.strip()
}
LRCAPI_URL = env("YT_LRCAPI_URL", "http://host.docker.internal:28883").rstrip("/")
LRCLIB_URL = env("YT_LRCLIB_URL", "https://lrclib.net/api/search")
PLAYLIST_MAX_ITEMS = max(1, int(env("YT_PLAYLIST_MAX_ITEMS", "50") or "50"))
VIDEO_MAX_HEIGHT = max(0, int(env("YT_VIDEO_MAX_HEIGHT", "0") or "0"))
VIDEO_FORMAT_SORT = env(
    "YT_VIDEO_FORMAT_SORT",
    "res,fps,hdr,quality,lang,tbr,vcodec,channels,acodec,asr",
)
VIDEO_MERGE_FORMAT = env("YT_VIDEO_MERGE_FORMAT", "mkv") or "mkv"
AUTOMATIC_SUBTITLE_LANGUAGES = env(
    "YT_AUTOMATIC_SUBTITLE_LANGUAGES",
    "zh-Hans,zh-Hant,zh-CN,zh-TW,zh-HK,zh,en-orig,en,.*-orig",
)
SUBTITLE_DOWNLOAD_TIMEOUT_SECONDS = max(
    60,
    int(env("YT_SUBTITLE_DOWNLOAD_TIMEOUT_SECONDS", "900") or "900"),
)
SUBTITLE_SLEEP_SECONDS = max(
    0.0,
    float(env("YT_SUBTITLE_SLEEP_SECONDS", "2") or "2"),
)
SUBTITLE_RATE_LIMIT_RETRIES = max(
    1,
    int(env("YT_SUBTITLE_RATE_LIMIT_RETRIES", "4") or "4"),
)
SUBTITLE_RATE_LIMIT_RETRY_SECONDS = max(
    5,
    int(env("YT_SUBTITLE_RATE_LIMIT_RETRY_SECONDS", "60") or "60"),
)
BILINGUAL_CHINESE_FONT = env("YT_BILINGUAL_CHINESE_FONT", "PingFang SC")
BILINGUAL_ENGLISH_FONT = env("YT_BILINGUAL_ENGLISH_FONT", "Arial")
BILINGUAL_CHINESE_SIZE = max(
    16,
    int(env("YT_BILINGUAL_CHINESE_SIZE", "68") or "68"),
)
BILINGUAL_ENGLISH_SIZE = max(
    12,
    int(env("YT_BILINGUAL_ENGLISH_SIZE", "42") or "42"),
)
BILINGUAL_MARGIN_V = max(
    0,
    int(env("YT_BILINGUAL_MARGIN_V", "64") or "64"),
)
BILINGUAL_MARGIN_H = max(
    0,
    int(env("YT_BILINGUAL_MARGIN_H", "96") or "96"),
)
BILINGUAL_SAFE_WIDTH = max(
    960,
    int(env("YT_BILINGUAL_SAFE_WIDTH", "1728") or "1728"),
)
BILINGUAL_CUE_GAP_MS = max(
    10,
    int(env("YT_BILINGUAL_CUE_GAP_MS", "80") or "80"),
)
BILINGUAL_CHINESE_COLOR = "&H00FFFFFF&"
BILINGUAL_ENGLISH_COLOR = "&H00A6E4FF&"
BILINGUAL_OUTLINE_COLOR = "&H00101010&"
BILINGUAL_SHADOW_COLOR = "&H80000000&"
BILINGUAL_OUTLINE_SIZE = 3
BILINGUAL_SHADOW_SIZE = 1
BILINGUAL_CHINESE_MIN_SIZE = 42
BILINGUAL_ENGLISH_MIN_SIZE = 34
BILINGUAL_CHINESE_MAX_CHARS = 16
BILINGUAL_ENGLISH_MAX_CHARS = 42
BILINGUAL_CHINESE_MAX_CPS = 9
BILINGUAL_ENGLISH_MAX_CPS = 17
BILINGUAL_MIN_DURATION_MS = 830
THUMBNAIL_DOWNLOAD_TIMEOUT_SECONDS = max(
    10,
    int(env("ONLINE_MEDIA_THUMBNAIL_TIMEOUT_SECONDS", "45") or "45"),
)
THUMBNAIL_MAX_BYTES = max(
    1_048_576,
    int(env("ONLINE_MEDIA_THUMBNAIL_MAX_BYTES", "26214400") or "26214400"),
)
OUTPUT_UID = int(env("YT_OUTPUT_UID", "1027") or "1027")
OUTPUT_GID = int(env("YT_OUTPUT_GID", "100") or "100")
OUTPUT_FILE_MODE = int(env("YT_OUTPUT_FILE_MODE", "0664") or "0664", 8)
OUTPUT_DIR_MODE = int(env("YT_OUTPUT_DIR_MODE", "0775") or "0775", 8)
PROBE_TIMEOUT_SECONDS = max(15, int(env("YT_PROBE_TIMEOUT_SECONDS", "60") or "60"))
DOWNLOAD_TIMEOUT_SECONDS = max(
    300,
    int(env("YT_DOWNLOAD_TIMEOUT_SECONDS", "21600") or "21600"),
)
BEETS_ENABLED = env_flag("YT_BEETS_ENABLED", True)
LRC_ENABLED = env_flag("YT_LRC_ENABLED", True)
NAVIDROME_URL = env("NAVIDROME_URL", "http://host.docker.internal:4534").rstrip("/")
NAVIDROME_USER = env("NAVIDROME_USER")
NAVIDROME_PASSWORD = env("NAVIDROME_PASSWORD")
EMBY_URL = env("EMBY_URL", "http://host.docker.internal:8096").rstrip("/")
EMBY_USER = env("EMBY_USER")
EMBY_PASSWORD = env("EMBY_PASSWORD")
EMBY_METADATA_SYNC_ENABLED = env_flag("EMBY_METADATA_SYNC_ENABLED", True)
EMBY_METADATA_SYNC_TIMEOUT_SECONDS = max(
    15,
    int(env("EMBY_METADATA_SYNC_TIMEOUT_SECONDS", "120") or "120"),
)
EMBY_YOUTUBE_LIBRARY_PATH = env("EMBY_YOUTUBE_LIBRARY_PATH", "/video/YouTube")
DB_PATH = STATE_DIR / "downloads.sqlite3"
BEETS_DIR = STATE_DIR / "beets"
BEETS_CONFIG = BEETS_DIR / "config.yaml"


@dataclass(frozen=True)
class ProbeResult:
    url: str
    title: str
    uploader: str
    item_count: int
    is_collection: bool
    truncated: bool = False
    auth_strategy: str = "anonymous-standard"


@dataclass(frozen=True)
class DownloadResult:
    kind: str
    title: str
    added_paths: list[str]
    existing_paths: list[str]
    metadata_matches: int
    lyrics_added: int
    log_file: str
    scan_triggered: bool
    emby_metadata_synced: int = 0

    @property
    def added_count(self) -> int:
        return len(self.added_paths)

    @property
    def existing_count(self) -> int:
        return len(self.existing_paths)


class YouTubeIngestError(RuntimeError):
    pass


@dataclass(frozen=True)
class YouTubeAuthStrategy:
    name: str
    use_cookies: bool
    use_po_token: bool
    player_client: str = ""


@dataclass(frozen=True)
class SubtitleCue:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class BilingualSubtitleCue:
    start_ms: int
    end_ms: int
    secondary: str
    chinese: str


def validate_url(url: str) -> str:
    url = url.strip().rstrip(TRAILING_PUNCTUATION)
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or host not in YOUTUBE_HOSTS:
        raise ValueError("只允许 YouTube 或 YouTube Music 的 http/https 链接")
    if parsed.username or parsed.password:
        raise ValueError("YouTube 链接不能包含用户名或密码")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("YouTube 链接使用了不允许的端口")

    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    tracking = {"si", "feature", "pp", "utm_source", "utm_medium", "utm_campaign"}
    clean_query = urllib.parse.urlencode([(key, value) for key, value in query if key not in tracking])
    return urllib.parse.urlunparse(
        ("https", parsed.netloc, parsed.path or "/", parsed.params, clean_query, "")
    )


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in GENERIC_URL_RE.finditer(text):
        try:
            url = validate_url(match.group(0))
        except (ValueError, urllib.error.URLError):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def is_music_url(url: str) -> bool:
    return (urllib.parse.urlparse(url).hostname or "").lower() == "music.youtube.com"


def is_collection_hint(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/").lower()
    if path in {"/playlist", "/feed/subscriptions"}:
        return True
    if path.startswith(("/channel/", "/c/", "/user/", "/@", "/hashtag/")):
        return True
    return False


def _cookie_names() -> set[str]:
    if not COOKIE_PATH.is_file() or COOKIE_PATH.stat().st_size == 0:
        return set()
    names: set[str] = set()
    try:
        for raw_line in COOKIE_PATH.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines():
            line = raw_line
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_") :]
            elif not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and parts[5]:
                names.add(parts[5])
    except OSError:
        return set()
    return names


def _cookie_health() -> tuple[set[str], set[str]]:
    names = _cookie_names()
    return names, REQUIRED_COOKIE_NAMES - names


class _DockerSocketConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float = 10) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.socket_path))


def _po_token_provider_healthy(timeout: float = 3) -> bool:
    if not PO_TOKEN_ENABLED or not PO_TOKEN_PROVIDER_URL:
        return True
    request = urllib.request.Request(
        f"{PO_TOKEN_PROVIDER_URL}/ping",
        headers={"user-agent": "YouTubeIngest/1.0"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def _docker_container_action(action: str) -> bool:
    if (
        action not in {"start", "restart"}
        or not PO_TOKEN_CONTAINER_NAME
        or not DOCKER_SOCKET_PATH.exists()
    ):
        return False
    container = urllib.parse.quote(PO_TOKEN_CONTAINER_NAME, safe="")
    suffix = "?t=5" if action == "restart" else ""
    connection = _DockerSocketConnection(DOCKER_SOCKET_PATH, timeout=10)
    try:
        connection.request(
            "POST",
            f"/containers/{container}/{action}{suffix}",
            body=b"",
            headers={"content-length": "0"},
        )
        response = connection.getresponse()
        response.read()
        return response.status in {204, 304}
    except Exception:
        return False
    finally:
        connection.close()


def _wait_for_po_token_provider(seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _po_token_provider_healthy():
            return True
        time.sleep(1)
    return _po_token_provider_healthy()


def _ensure_po_token_provider() -> None:
    if not PO_TOKEN_ENABLED:
        return
    if _po_token_provider_healthy():
        return
    _docker_container_action("start")
    if _wait_for_po_token_provider(12):
        return
    _docker_container_action("restart")
    if _wait_for_po_token_provider(20):
        return
    raise YouTubeIngestError(
        "YouTube PO Token 服务不可用，自动启动和重启均未恢复；"
        "为避免悄悄降级到低清格式，本次任务已停止。"
    )


def _youtube_auth_strategies() -> list[YouTubeAuthStrategy]:
    _, missing = _cookie_health()
    cookie_ready = not missing and COOKIE_PATH.is_file()
    strategies: list[YouTubeAuthStrategy] = []
    if PO_TOKEN_ENABLED and PO_TOKEN_PROVIDER_URL:
        if cookie_ready:
            strategies.append(
                YouTubeAuthStrategy(
                    name="cookies-po-token",
                    use_cookies=True,
                    use_po_token=True,
                    player_client=PLAYER_CLIENT,
                )
            )
            strategies.append(
                YouTubeAuthStrategy(
                    name="cookies-embedded",
                    use_cookies=True,
                    use_po_token=False,
                    player_client="web_embedded",
                )
            )
        strategies.append(
            YouTubeAuthStrategy(
                name="anonymous-embedded",
                use_cookies=False,
                use_po_token=False,
                player_client="web_embedded",
            )
        )
        strategies.append(
            YouTubeAuthStrategy(
                name="anonymous-po-token",
                use_cookies=False,
                use_po_token=True,
                player_client=PLAYER_CLIENT,
            )
        )
        return strategies
    else:
        if cookie_ready:
            strategies.append(
                YouTubeAuthStrategy(
                    name="cookies-standard",
                    use_cookies=True,
                    use_po_token=False,
                    player_client=PLAYER_CLIENT,
                )
            )
        strategies.append(
            YouTubeAuthStrategy(
                name="anonymous-standard",
                use_cookies=False,
                use_po_token=False,
                player_client=PLAYER_CLIENT,
            )
        )
    return strategies


def _ordered_auth_strategies(preferred: str = "") -> list[YouTubeAuthStrategy]:
    strategies = _youtube_auth_strategies()
    if not preferred:
        return strategies
    selected = [strategy for strategy in strategies if strategy.name == preferred]
    return [*selected, *(strategy for strategy in strategies if strategy.name != preferred)]


def _common_yt_dlp_args(
    strategy: YouTubeAuthStrategy,
    proxy_url: str | None = None,
) -> list[str]:
    selected_proxy = PROXY_URL if proxy_url is None else proxy_url
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-color",
        "--no-warnings",
        "--socket-timeout",
        "30",
        "--retries",
        "10",
        "--fragment-retries",
        "10",
        "--extractor-retries",
        "5",
        "--js-runtimes",
        "deno",
    ]
    if selected_proxy:
        command.extend(["--proxy", selected_proxy])
    if strategy.use_cookies:
        command.extend(["--cookies", str(COOKIE_PATH)])
    player_client = strategy.player_client or PLAYER_CLIENT
    if player_client:
        command.extend(
            [
                "--extractor-args",
                f"youtube:player_client={player_client}",
            ]
        )
    if strategy.use_po_token:
        command.extend(
            [
                "--extractor-args",
                f"youtubepot-bgutilhttp:base_url={PO_TOKEN_PROVIDER_URL}",
            ]
        )
    return command


AUTH_ERROR_MARKERS = (
    "sign in to confirm you",
    "login_required",
    "login required",
    "authentication required",
    "cookies are no longer valid",
    "account cookies",
    "confirm you’re not a bot",
    "confirm you're not a bot",
)
PO_TOKEN_ERROR_MARKERS = (
    "po token",
    "pot provider",
    "bgutil",
    "no video formats found",
    "requested format is not available",
    "http error 403",
    "403: forbidden",
)
NETWORK_ERROR_MARKERS = (
    "unexpected_eof_while_reading",
    "eof occurred in violation of protocol",
    "ssl_error_syscall",
    "connection reset by peer",
    "connection aborted",
    "remote end closed connection",
    "remote disconnected",
    "incomplete read",
    "read timed out",
    "connection timed out",
    "network is unreachable",
    "temporary failure in name resolution",
    "name or service not known",
    "connection refused",
    "unable to connect to proxy",
    "tunnel connection failed",
)
YT_DLP_PROGRESS_PREFIX = "__AMDL_PROGRESS__"
ProcessObserver = Callable[[subprocess.Popen[str] | None], None]
ProgressObserver = Callable[[dict[str, Any]], None]


def _notify_process(
    observer: ProcessObserver | None,
    process: subprocess.Popen[str] | None,
) -> None:
    if observer is not None:
        observer(process)


def _notify_progress(
    observer: ProgressObserver | None,
    **values: Any,
) -> None:
    if observer is not None:
        observer(values)


def _progress_number(value: str) -> float | None:
    normalized = value.strip()
    if not normalized or normalized.casefold() in {"na", "none", "unknown"}:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def _format_progress_eta(value: str) -> str:
    seconds = _progress_number(value)
    if seconds is None:
        return ""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (
        f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        if hours
        else f"{minutes:02d}:{seconds:02d}"
    )


def _parse_yt_dlp_progress(line: str) -> dict[str, Any] | None:
    marker_index = line.find(YT_DLP_PROGRESS_PREFIX)
    if marker_index < 0:
        return None
    fields = line[marker_index + len(YT_DLP_PROGRESS_PREFIX) :].strip().split("|")
    if len(fields) != 5:
        return None
    downloaded = _progress_number(fields[0])
    exact_total = _progress_number(fields[1])
    estimated_total = _progress_number(fields[2])
    speed = _progress_number(fields[3])
    total = exact_total or estimated_total
    values: dict[str, Any] = {"phase": "下载中"}
    if downloaded is not None:
        values["downloaded_bytes"] = max(0, round(downloaded))
    if total is not None and total > 0:
        values["total_bytes"] = max(0, round(total))
        values["total_estimated"] = int(exact_total is None)
        if downloaded is not None:
            values["percent"] = min(99.5, max(0.0, downloaded * 100 / total))
    if speed is not None:
        values["speed_bps"] = max(0.0, speed)
    eta = _format_progress_eta(fields[4])
    if eta:
        values["eta"] = eta
    return values


def _is_retryable_youtube_error(detail: str) -> bool:
    normalized = detail.casefold()
    return any(
        marker in normalized
        for marker in (*AUTH_ERROR_MARKERS, *PO_TOKEN_ERROR_MARKERS)
    )


def _is_retryable_youtube_network_error(detail: str) -> bool:
    normalized = detail.casefold()
    return any(marker in normalized for marker in NETWORK_ERROR_MARKERS)


def _is_youtube_auth_error(detail: str) -> bool:
    normalized = detail.casefold()
    return any(marker in normalized for marker in AUTH_ERROR_MARKERS)


def _refresh_youtube_auth(url: str) -> tuple[bool, str]:
    if AUTH_AUTO_REFRESH_DISABLED:
        return False, "本次 Cookie 候选校验不允许递归自动刷新"
    from auth_auto_refresh import refresh_auth

    result = refresh_auth("youtube", url)
    return result.success, result.detail


def _last_error(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else "未知错误"


def probe_url(
    url: str,
    preferred_auth_strategy: str = "",
    allow_auth_fallback: bool = True,
    _auto_refresh_attempted: bool = False,
) -> ProbeResult:
    url = validate_url(url)
    _ensure_po_token_provider()
    collection = is_collection_hint(url)
    failures: list[tuple[str, str]] = []
    strategies = _ordered_auth_strategies(preferred_auth_strategy)
    if preferred_auth_strategy and not allow_auth_fallback:
        strategies = [
            strategy
            for strategy in strategies
            if strategy.name == preferred_auth_strategy
        ]
        if not strategies:
            raise YouTubeIngestError(
                f"YouTube 验证策略不可用：{preferred_auth_strategy}"
            )
    for strategy in strategies:
        command = [
            *_common_yt_dlp_args(strategy),
            "--dump-single-json",
            "--flat-playlist",
            "--skip-download",
            "--playlist-end",
            str(PLAYLIST_MAX_ITEMS + 1),
        ]
        if not collection:
            command.append("--no-playlist")
        command.append(url)
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            failures.append(
                (
                    strategy.name,
                    f"读取超时（{PROBE_TIMEOUT_SECONDS} 秒）",
                )
            )
            continue
        if completed.returncode != 0:
            failures.append((strategy.name, _last_error(completed.stderr)))
            continue
        try:
            info = json.loads(completed.stdout)
        except json.JSONDecodeError:
            failures.append((strategy.name, "yt-dlp 没有返回有效的链接信息"))
            continue

        entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
        reported_count = int(info.get("playlist_count") or info.get("n_entries") or 0)
        item_count = max(reported_count, len(entries), 1)
        truncated = item_count > PLAYLIST_MAX_ITEMS or len(entries) > PLAYLIST_MAX_ITEMS
        return ProbeResult(
            url=url,
            title=str(info.get("title") or info.get("fulltitle") or "YouTube 内容").strip(),
            uploader=str(info.get("uploader") or info.get("channel") or "").strip(),
            item_count=item_count,
            is_collection=bool(entries) or collection,
            truncated=truncated,
            auth_strategy=strategy.name,
        )

    names, missing = _cookie_health()
    last_detail = failures[-1][1] if failures else "未知错误"
    auth_failure = any(_is_youtube_auth_error(detail) for _, detail in failures)
    refresh_detail = ""
    if (
        auth_failure
        and not _auto_refresh_attempted
        and not AUTH_AUTO_REFRESH_DISABLED
    ):
        refreshed, refresh_detail = _refresh_youtube_auth(url)
        if refreshed:
            return probe_url(
                url,
                preferred_auth_strategy=preferred_auth_strategy,
                allow_auth_fallback=allow_auth_fallback,
                _auto_refresh_attempted=True,
            )
    if auth_failure and missing:
        missing_text = ", ".join(sorted(missing))
        raise YouTubeIngestError(
            "YouTube 要求登录验证，NAS Cookie 登录链不完整。"
            f"当前读取 {len(names)} 项，缺少：{missing_text}。"
            + (
                f"自动恢复结果：{refresh_detail}"
                if refresh_detail
                else "请启动 NAS YouTube 登录浏览器完成一次登录并导出 Cookie。"
            )
        )
    if auth_failure and not missing:
        raise YouTubeIngestError(
            "YouTube 拒绝了当前 NAS 登录会话，Cookie 可能已过期或被轮换。"
            f"上游返回：{last_detail}。"
            + (
                f"自动恢复结果：{refresh_detail}"
                if refresh_detail
                else "请启动 NAS YouTube 登录浏览器重新登录并导出。"
            )
        )
    attempts = ", ".join(name for name, _ in failures)
    raise YouTubeIngestError(
        f"无法读取 YouTube 链接（已尝试：{attempts or '无'}）：{last_detail}"
    )


def _database() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(DB_PATH)
    database.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            kind TEXT NOT NULL,
            video_id TEXT NOT NULL,
            source_url TEXT NOT NULL,
            title TEXT NOT NULL,
            output_path TEXT NOT NULL,
            metadata_matched INTEGER NOT NULL DEFAULT 0,
            lyrics_added INTEGER NOT NULL DEFAULT 0,
            downloaded_at REAL NOT NULL,
            PRIMARY KEY (kind, video_id)
        )
        """
    )
    database.commit()
    return database


def _existing_downloads(kind: str, video_ids: list[str]) -> list[str]:
    if not video_ids or not DB_PATH.exists():
        return []
    with _database() as database:
        paths: list[str] = []
        for video_id in video_ids:
            row = database.execute(
                "SELECT output_path FROM downloads WHERE kind=? AND video_id=?",
                (kind, video_id),
            ).fetchone()
            if row and Path(row[0]).exists():
                paths.append(str(row[0]))
        return paths


def _record_download(
    kind: str,
    video_id: str,
    source_url: str,
    title: str,
    output_path: Path,
    metadata_matched: bool,
    lyrics_added: bool,
) -> None:
    with _database() as database:
        database.execute(
            """
            INSERT INTO downloads (
                kind, video_id, source_url, title, output_path,
                metadata_matched, lyrics_added, downloaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, video_id) DO UPDATE SET
                source_url=excluded.source_url,
                title=excluded.title,
                output_path=excluded.output_path,
                metadata_matched=excluded.metadata_matched,
                lyrics_added=excluded.lyrics_added,
                downloaded_at=excluded.downloaded_at
            """,
            (
                kind,
                video_id,
                source_url,
                title,
                str(output_path),
                int(metadata_matched),
                int(lyrics_added),
                time.time(),
            ),
        )
        database.commit()


def _safe_component(value: Any, fallback: str, limit: int = 120) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    # Emby parses an ASCII pipe in a local image path as a separator, which
    # truncates poster paths. Preserve the punctuation using its full-width form.
    text = text.replace("|", "｜")
    text = re.sub(r"[\x00-\x1f\x7f/\\:]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = fallback
    if len(text) > limit:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        text = f"{text[: limit - 11].rstrip()} [{digest}]"
    # ext4/btrfs filenames are limited by bytes, not Unicode characters.
    # Leave room for source IDs and extensions appended by callers.
    byte_limit = 200
    encoded = text.encode("utf-8")
    if len(encoded) > byte_limit:
        digest = hashlib.sha1(encoded).hexdigest()[:8]
        suffix = f" [{digest}]"
        budget = byte_limit - len(suffix.encode("utf-8"))
        prefix = encoded[:budget]
        while prefix:
            try:
                text = prefix.decode("utf-8").rstrip() + suffix
                break
            except UnicodeDecodeError:
                prefix = prefix[:-1]
        if not prefix:
            text = fallback
    return text


def _first(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return str(value[0]).strip() if value else ""
    return str(value or "").strip()


def _thumbnail_candidate_urls(info: dict[str, Any]) -> list[str]:
    selected_url = _first(info.get("thumbnail"))
    candidates: list[tuple[tuple[int, int, int, float], str]] = []
    seen: set[str] = set()
    for thumbnail in info.get("thumbnails") or []:
        if not isinstance(thumbnail, dict):
            continue
        url = _first(thumbnail.get("url"))
        if not url or url in seen:
            continue
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        seen.add(url)
        try:
            width = max(0, int(thumbnail.get("width") or 0))
            height = max(0, int(thumbnail.get("height") or 0))
            preference = float(thumbnail.get("preference") or 0)
        except (TypeError, ValueError):
            width = height = 0
            preference = 0
        candidates.append(
            (
                (
                    int(url == selected_url),
                    width * height,
                    int(parsed.path.casefold().endswith((".jpg", ".jpeg"))),
                    preference,
                ),
                url,
            )
        )
    if selected_url and selected_url not in seen:
        parsed = urllib.parse.urlparse(selected_url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            candidates.append(
                (
                    (
                        1,
                        0,
                        int(parsed.path.casefold().endswith((".jpg", ".jpeg"))),
                        0,
                    ),
                    selected_url,
                )
            )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [url for _, url in candidates]


def _thumbnail_image_extension(payload: bytes) -> str:
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "webp"
    return ""


def _image_dimensions(path: Path) -> tuple[int, int]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(path),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    match = re.fullmatch(r"(\d+)x(\d+)", completed.stdout.strip())
    if completed.returncode != 0 or not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def _download_best_thumbnail(
    info: dict[str, Any],
    temp_dir: Path,
    media_id: str,
    proxy_url: str,
    log_handle: Any,
) -> Path | None:
    final_path = temp_dir / f"{media_id}.source.jpg"
    referer = (
        _first(info.get("webpage_url"))
        or _first(info.get("original_url"))
        or _first(info.get("url"))
    )
    handlers: list[Any] = []
    if proxy_url:
        handlers.append(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
    opener = urllib.request.build_opener(*handlers)
    errors: list[str] = []
    for url in _thumbnail_candidate_urls(info):
        parsed = urllib.parse.urlparse(url)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        if referer.startswith(("http://", "https://")):
            headers["Referer"] = referer
        raw_path: Path | None = None
        converted_path: Path | None = None
        try:
            request = urllib.request.Request(url, headers=headers)
            with opener.open(
                request,
                timeout=THUMBNAIL_DOWNLOAD_TIMEOUT_SECONDS,
            ) as response:
                reported_size = int(response.headers.get("content-length") or 0)
                if reported_size > THUMBNAIL_MAX_BYTES:
                    raise ValueError(f"image is {reported_size} bytes")
                payload = response.read(THUMBNAIL_MAX_BYTES + 1)
            if len(payload) > THUMBNAIL_MAX_BYTES:
                raise ValueError(f"image exceeds {THUMBNAIL_MAX_BYTES} bytes")
            extension = _thumbnail_image_extension(payload)
            if not extension:
                raise ValueError("response is not a supported image")

            token = secrets.token_hex(4)
            raw_path = temp_dir / f".{media_id}.thumbnail-{token}.{extension}"
            raw_path.write_bytes(payload)
            if extension == "jpg":
                os.replace(raw_path, final_path)
                raw_path = None
            else:
                converted_path = temp_dir / f".{media_id}.thumbnail-{token}.jpg"
                completed = subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(raw_path),
                        "-frames:v",
                        "1",
                        "-q:v",
                        "1",
                        str(converted_path),
                    ],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                )
                if completed.returncode != 0 or not converted_path.is_file():
                    raise ValueError(
                        completed.stderr.strip().splitlines()[-1]
                        if completed.stderr.strip()
                        else "ffmpeg conversion failed"
                    )
                os.replace(converted_path, final_path)
                converted_path = None

            width, height = _image_dimensions(final_path)
            if width <= 0 or height <= 0:
                raise ValueError("downloaded image has invalid dimensions")
            log_handle.write(
                "thumbnail: saved original highest-quality source "
                f"{width}x{height}, {final_path.stat().st_size} bytes, "
                f"host={parsed.hostname}\n"
            )
            return final_path
        except Exception as exc:
            final_path.unlink(missing_ok=True)
            errors.append(f"{parsed.hostname or 'unknown'}: {exc}")
        finally:
            if raw_path is not None:
                raw_path.unlink(missing_ok=True)
            if converted_path is not None:
                converted_path.unlink(missing_ok=True)
    if errors:
        log_handle.write(
            "thumbnail: original source download failed; using yt-dlp fallback: "
            + " | ".join(errors[-3:])
            + "\n"
        )
    return None


def _metadata_from_info(info: dict[str, Any]) -> dict[str, Any]:
    title = _first(info.get("track")) or _first(info.get("title")) or "Unknown Title"
    artist = (
        _first(info.get("artist"))
        or _first(info.get("creator"))
        or _first(info.get("uploader"))
        or "Unknown Artist"
    )
    album = _first(info.get("album"))
    if not album and info.get("playlist") and not str(info.get("playlist")).startswith("Uploads from"):
        album = _first(info.get("playlist"))
    if not album:
        album = "Singles"
    year = _first(info.get("release_year"))
    if not year:
        date = _first(info.get("release_date")) or _first(info.get("upload_date"))
        year = date[:4] if len(date) >= 4 else ""
    try:
        track_number = int(info.get("track_number") or info.get("playlist_index") or 0)
    except (TypeError, ValueError):
        track_number = 0
    categories = info.get("categories") or []
    genre = _first(info.get("genre")) or _first(categories) or "YouTube Music"
    return {
        "title": title,
        "artist": artist,
        "album_artist": _first(info.get("album_artist")) or artist,
        "album": album,
        "year": year,
        "track_number": track_number,
        "genre": genre,
    }


def _write_initial_audio_tags(
    path: Path,
    metadata: dict[str, Any],
    info: dict[str, Any],
    source_name: str = "YouTube",
) -> None:
    if path.suffix.lower() != ".m4a":
        return
    audio = MP4(path)
    audio["\xa9nam"] = [metadata["title"]]
    audio["\xa9ART"] = [metadata["artist"]]
    audio["aART"] = [metadata["album_artist"]]
    audio["\xa9alb"] = [metadata["album"]]
    audio["\xa9gen"] = [metadata["genre"]]
    if metadata["year"]:
        audio["\xa9day"] = [metadata["year"]]
    if metadata["track_number"]:
        audio["trkn"] = [(metadata["track_number"], 0)]
    source_url = _first(info.get("webpage_url")) or f"https://www.youtube.com/watch?v={info.get('id', '')}"
    audio["\xa9cmt"] = [f"{source_name} ID: {info.get('id', '')}\nSource: {source_url}"]
    audio.save()


def _ensure_beets_config() -> None:
    BEETS_DIR.mkdir(parents=True, exist_ok=True)
    if BEETS_CONFIG.exists():
        return
    BEETS_CONFIG.write_text(
        """directory: /youtube-music
library: /state/youtube/beets/library.db
import:
  write: yes
  copy: no
  move: no
  resume: no
  incremental: no
  quiet: yes
  quiet_fallback: skip
  default_action: skip
match:
  strong_rec_thresh: 0.04
  medium_rec_thresh: 0.10
  rec_gap_thresh: 0.25
ui:
  color: no
""",
        encoding="utf-8",
    )


def _read_easy_tags(path: Path) -> dict[str, str]:
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return {}
    if audio is None or audio.tags is None:
        return {}
    result: dict[str, str] = {}
    for key in ("title", "artist", "albumartist", "album", "date", "tracknumber", "genre", "musicbrainz_trackid"):
        value = audio.tags.get(key)
        if value:
            result[key] = _first(value)
    return result


def _enrich_with_beets(path: Path, log_handle: Any) -> bool:
    if not BEETS_ENABLED:
        return False
    _ensure_beets_config()
    before = _read_easy_tags(path)
    process_env = os.environ.copy()
    if PROXY_URL:
        process_env.update(
            {
                "HTTP_PROXY": PROXY_URL,
                "HTTPS_PROXY": PROXY_URL,
                "http_proxy": PROXY_URL,
                "https_proxy": PROXY_URL,
            }
        )
    try:
        completed = subprocess.run(
            ["beet", "-c", str(BEETS_CONFIG), "import", "-q", "-s", str(path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env=process_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log_handle.write(f"beets metadata lookup skipped: {exc}\n")
        return False
    if completed.stdout:
        log_handle.write(completed.stdout)
    after = _read_easy_tags(path)
    return bool(after.get("musicbrainz_trackid")) or (
        completed.returncode == 0
        and any(after.get(key) and after.get(key) != before.get(key) for key in ("title", "artist", "album"))
    )


def _fetch_lrc(metadata: dict[str, Any], timeout: int = 45) -> str:
    if not LRC_ENABLED:
        return ""
    lyrics = ""
    if LRCAPI_URL:
        query = urllib.parse.urlencode(
            {
                "title": metadata["title"],
                "artist": metadata["artist"],
                "album": metadata["album"],
            }
        )
        request = urllib.request.Request(
            f"{LRCAPI_URL}/lyrics?{query}",
            headers={"user-agent": "YouTubeIngest/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                lyrics = response.read().decode("utf-8", errors="replace").strip()
        except Exception:
            pass
    if LRC_TIMESTAMP_RE.search(lyrics):
        return lyrics + ("\n" if not lyrics.endswith("\n") else "")

    if not LRCLIB_URL:
        return ""
    query = urllib.parse.urlencode(
        {
            "track_name": metadata["title"],
            "artist_name": metadata["artist"],
            "album_name": metadata["album"],
        }
    )
    request = urllib.request.Request(
        f"{LRCLIB_URL}?{query}",
        headers={"user-agent": "YouTubeIngest/1.0 (Navidrome lyrics importer)"},
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY_URL, "https": PROXY_URL})
        if PROXY_URL
        else urllib.request.ProxyHandler({})
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            candidates = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""

    def normalized(value: Any) -> str:
        value = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)

    wanted_title = normalized(metadata["title"])
    wanted_artist = normalized(metadata["artist"])
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict):
            continue
        title = normalized(candidate.get("trackName"))
        artist = normalized(candidate.get("artistName"))
        artist_matches = bool(
            wanted_artist
            and artist
            and (wanted_artist == artist or wanted_artist in artist or artist in wanted_artist)
        )
        synced = str(candidate.get("syncedLyrics") or "").strip()
        if title == wanted_title and artist_matches and LRC_TIMESTAMP_RE.search(synced):
            return synced + ("\n" if not synced.endswith("\n") else "")
    return ""


def _final_audio_metadata(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    tags = _read_easy_tags(path)
    track_text = tags.get("tracknumber", "").split("/", 1)[0]
    try:
        track_number = int(track_text or fallback["track_number"] or 0)
    except ValueError:
        track_number = int(fallback["track_number"] or 0)
    return {
        "title": tags.get("title") or fallback["title"],
        "artist": tags.get("artist") or fallback["artist"],
        "album_artist": tags.get("albumartist") or tags.get("artist") or fallback["album_artist"],
        "album": tags.get("album") or fallback["album"],
        "year": (tags.get("date") or fallback["year"])[:4],
        "track_number": track_number,
        "genre": tags.get("genre") or fallback["genre"],
    }


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise YouTubeIngestError(f"无法为文件生成不重复的名称：{path}")


def _unique_directory(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.name} ({index})")
        if not candidate.exists():
            return candidate
    raise YouTubeIngestError(f"无法为目录生成不重复的名称：{path}")


def _set_output_permissions(path: Path, root: Path) -> None:
    def apply(target: Path, mode: int) -> None:
        try:
            os.chown(target, OUTPUT_UID, OUTPUT_GID)
            os.chmod(target, mode)
        except OSError:
            pass

    if path.is_dir():
        for directory, _subdirs, files in os.walk(path):
            directory_path = Path(directory)
            apply(directory_path, OUTPUT_DIR_MODE)
            for filename in files:
                apply(directory_path / filename, OUTPUT_FILE_MODE)
        parent = path.parent
    else:
        apply(path, OUTPUT_FILE_MODE)
        parent = path.parent

    root = root.resolve()
    while parent != root and root in parent.resolve().parents:
        apply(parent, OUTPUT_DIR_MODE)
        parent = parent.parent


def _move_audio(
    media_path: Path,
    info: dict[str, Any],
    log_handle: Any,
) -> tuple[Path, bool, bool]:
    initial_metadata = _metadata_from_info(info)
    _write_initial_audio_tags(media_path, initial_metadata, info)
    metadata_matched = _enrich_with_beets(media_path, log_handle)
    metadata = _final_audio_metadata(media_path, initial_metadata)

    artist = _safe_component(metadata["album_artist"], "Unknown Artist")
    album = _safe_component(metadata["album"], "Singles")
    title = _safe_component(metadata["title"], "Unknown Title", limit=150)
    video_id = _safe_component(info.get("id"), "unknown", limit=32)
    track = int(metadata["track_number"] or 0)
    filename = f"{track:02d} - {title} [YT_{video_id}]{media_path.suffix.lower()}" if track else f"{title} [YT_{video_id}]{media_path.suffix.lower()}"
    destination_dir = AUDIO_ROOT / artist / album
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / filename
    if destination.exists() and destination.stat().st_size == media_path.stat().st_size:
        media_path.unlink(missing_ok=True)
    else:
        destination = _unique_path(destination)
        shutil.move(str(media_path), str(destination))

    lyrics = _fetch_lrc(metadata)
    lyrics_added = False
    if lyrics:
        lyric_path = destination.with_suffix(".lrc")
        lyric_path.write_text(lyrics, encoding="utf-8")
        _set_output_permissions(lyric_path, AUDIO_ROOT)
        lyrics_added = True
    _set_output_permissions(destination, AUDIO_ROOT)
    return destination, metadata_matched, lyrics_added


def _nfo_text(info: dict[str, Any], title: str, video_id: str, poster_name: str) -> str:
    root = ET.Element("movie")
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "originaltitle").text = title
    ET.SubElement(root, "sorttitle").text = title
    ET.SubElement(root, "plot").text = _first(info.get("description"))
    upload_date = _first(info.get("upload_date"))
    if len(upload_date) == 8:
        premiered = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
        ET.SubElement(root, "premiered").text = premiered
        ET.SubElement(root, "year").text = upload_date[:4]
    duration = int(float(info.get("duration") or 0))
    ET.SubElement(root, "runtime").text = str(max(1, round(duration / 60))) if duration else "0"
    ET.SubElement(root, "studio").text = _first(info.get("uploader")) or _first(info.get("channel"))
    for genre in info.get("categories") or ["YouTube"]:
        ET.SubElement(root, "genre").text = _first(genre)
    for tag in (info.get("tags") or [])[:30]:
        ET.SubElement(root, "tag").text = _first(tag)
    unique = ET.SubElement(root, "uniqueid", {"type": "youtube", "default": "true"})
    unique.text = video_id
    ET.SubElement(root, "id").text = video_id
    ET.SubElement(root, "thumb", {"aspect": "poster"}).text = poster_name
    ET.SubElement(root, "website").text = _first(info.get("webpage_url")) or f"https://www.youtube.com/watch?v={video_id}"
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _srt_timestamp_ms(value: str) -> int:
    hours, minutes, seconds = value.replace(".", ",").split(":", 2)
    whole_seconds, milliseconds = seconds.split(",", 1)
    return (
        int(hours) * 3_600_000
        + int(minutes) * 60_000
        + int(whole_seconds) * 1_000
        + int(milliseconds[:3].ljust(3, "0"))
    )


def _format_srt_timestamp(value_ms: int) -> str:
    value_ms = max(0, value_ms)
    hours, remainder = divmod(value_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _read_srt(path: Path) -> list[SubtitleCue]:
    content = path.read_text(encoding="utf-8-sig", errors="replace")
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    cues: list[SubtitleCue] = []
    for block in re.split(r"\n\s*\n", content):
        lines = block.splitlines()
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        match = SRT_TIMING_RE.match(lines[timing_index])
        if not match:
            continue
        text = "\n".join(lines[timing_index + 1 :]).strip()
        if not text:
            continue
        cues.append(
            SubtitleCue(
                start_ms=_srt_timestamp_ms(match.group("start")),
                end_ms=_srt_timestamp_ms(match.group("end")),
                text=text,
            )
        )
    return cues


def _write_srt(path: Path, cues: list[SubtitleCue]) -> None:
    blocks = []
    for index, cue in enumerate(cues, 1):
        blocks.append(
            f"{index}\n"
            f"{_format_srt_timestamp(cue.start_ms)} --> {_format_srt_timestamp(cue.end_ms)}\n"
            f"{cue.text}"
        )
    path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")


def _format_ass_timestamp(value_ms: int) -> str:
    centiseconds = max(0, value_ms) // 10
    hours, centiseconds = divmod(centiseconds, 360_000)
    minutes, centiseconds = divmod(centiseconds, 6_000)
    seconds, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _clean_ass_text(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", "", value))
    return re.sub(r"\s+", " ", value).strip()


def _escape_ass_text(value: str) -> str:
    return (
        value.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
    )


def _subtitle_visual_units(value: str) -> float:
    return sum(
        1.0 if unicodedata.east_asian_width(character) in {"W", "F"} else 0.54
        for character in value
    )


def _fitted_ass_font_size(value: str, preferred_size: int, minimum_size: int) -> int:
    units = max(1.0, _subtitle_visual_units(value))
    single_line_size = int(BILINGUAL_SAFE_WIDTH / units)
    fitted = max(minimum_size, min(preferred_size, single_line_size))
    return fitted if fitted % 2 == 0 else max(minimum_size, fitted - 1)


def _synchronize_bilingual_cues(
    cues: list[BilingualSubtitleCue],
) -> tuple[list[BilingualSubtitleCue], int, int]:
    ordered = sorted(cues, key=lambda cue: (cue.start_ms, cue.end_ms))
    synchronized: list[BilingualSubtitleCue] = []
    clipped = 0
    dropped = 0
    for index, cue in enumerate(ordered):
        end_ms = cue.end_ms
        if (
            index + 1 < len(ordered)
            and end_ms > ordered[index + 1].start_ms - BILINGUAL_CUE_GAP_MS
        ):
            end_ms = ordered[index + 1].start_ms - BILINGUAL_CUE_GAP_MS
            clipped += 1
        if end_ms <= cue.start_ms:
            dropped += 1
            continue
        synchronized.append(
            BilingualSubtitleCue(
                start_ms=cue.start_ms,
                end_ms=end_ms,
                secondary=_clean_ass_text(cue.secondary),
                chinese=_clean_ass_text(cue.chinese),
            )
        )
    return synchronized, clipped, dropped


def _write_bilingual_ass(
    path: Path,
    cues: list[BilingualSubtitleCue],
    secondary_language: str,
) -> None:
    header = f"""[Script Info]
Title: YouTube synchronized Chinese + {secondary_language} bilingual subtitles
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Bilingual,{BILINGUAL_CHINESE_FONT},{BILINGUAL_CHINESE_SIZE},{BILINGUAL_CHINESE_COLOR},&H000000FF&,{BILINGUAL_OUTLINE_COLOR},{BILINGUAL_SHADOW_COLOR},0,0,0,0,100,100,0,0,1,{BILINGUAL_OUTLINE_SIZE},{BILINGUAL_SHADOW_SIZE},2,{BILINGUAL_MARGIN_H},{BILINGUAL_MARGIN_H},{BILINGUAL_MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogue_lines: list[str] = []
    for cue in cues:
        chinese = _escape_ass_text(cue.chinese)
        secondary = _escape_ass_text(cue.secondary)
        chinese_size = _fitted_ass_font_size(
            cue.chinese,
            BILINGUAL_CHINESE_SIZE,
            BILINGUAL_CHINESE_MIN_SIZE,
        )
        secondary_size = _fitted_ass_font_size(
            cue.secondary,
            BILINGUAL_ENGLISH_SIZE,
            BILINGUAL_ENGLISH_MIN_SIZE,
        )
        if chinese and secondary:
            text = (
                rf"{{\an2\q2\b0\i0\fsp0\fn{BILINGUAL_CHINESE_FONT}"
                rf"\fs{chinese_size}\c{BILINGUAL_CHINESE_COLOR}}}{chinese}"
                rf"\N{{\b0\i0\fsp0\fn{BILINGUAL_ENGLISH_FONT}"
                rf"\fs{secondary_size}\c{BILINGUAL_ENGLISH_COLOR}}}{secondary}"
            )
        elif chinese:
            text = (
                rf"{{\an2\q2\b0\i0\fsp0\fn{BILINGUAL_CHINESE_FONT}"
                rf"\fs{chinese_size}\c{BILINGUAL_CHINESE_COLOR}}}{chinese}"
            )
        elif secondary:
            text = (
                rf"{{\an2\q2\b0\i0\fsp0\fn{BILINGUAL_ENGLISH_FONT}"
                rf"\fs{secondary_size}\c{BILINGUAL_ENGLISH_COLOR}}}{secondary}"
            )
        else:
            continue
        dialogue_lines.append(
            "Dialogue: 0,"
            f"{_format_ass_timestamp(cue.start_ms)},"
            f"{_format_ass_timestamp(cue.end_ms)},"
            f"Bilingual,,0,0,0,,{text}"
        )
    path.write_text(header + "\n".join(dialogue_lines) + "\n", encoding="utf-8")


def _subtitle_language(path: Path, video_id: str) -> str:
    prefix = f"{video_id}."
    if not path.name.startswith(prefix) or not path.name.lower().endswith(".srt"):
        return ""
    return path.name[len(prefix) : -4]


def _pick_subtitle(
    subtitles: dict[str, Path],
    priorities: tuple[str, ...],
    prefix: str,
) -> tuple[str, Path] | None:
    by_lower = {language.casefold(): (language, path) for language, path in subtitles.items()}
    for language in priorities:
        match = by_lower.get(language.casefold())
        if match:
            return match
    prefix = prefix.casefold()
    return next(
        (
            (language, path)
            for language, path in subtitles.items()
            if language.casefold() == prefix or language.casefold().startswith(f"{prefix}-")
        ),
        None,
    )


def _normalize_language_code(value: Any) -> str:
    language = _first(value).casefold().replace("_", "-").strip("-")
    if language.endswith("-orig"):
        language = language[: -len("-orig")]
    language = re.sub(r"[^a-z0-9-]", "", language)
    aliases = {
        "iw": "he",
        "in": "id",
        "ji": "yi",
    }
    parts = language.split("-", 1)
    base = aliases.get(parts[0], parts[0]) if parts else ""
    return f"{base}-{parts[1]}" if len(parts) == 2 and parts[1] else base


def _language_base(value: Any) -> str:
    return _normalize_language_code(value).split("-", 1)[0]


def _is_english_language(value: Any) -> bool:
    return _language_base(value) == "en"


def _is_chinese_language(value: Any) -> bool:
    language = _normalize_language_code(value)
    return language == "zh" or language.startswith(("zh-", "cmn", "yue"))


ORIGINAL_LANGUAGE_BY_CHANNEL = {
    "itsuka japan": "ja",
    "maibaru travel": "ja",
    "it's time to travel🇯🇵 / 旅する時間": "ja",
    "shushu": "ja",
    "わた旅": "ja",
    "佐田志歩のバリバリ伝説": "ja",
    "unique japan travel": "en",
    "sol life": "en",
    "ross cameron - warrior trading": "en",
    "sophia carey": "en",
}


def _inferred_original_language(info: dict[str, Any]) -> str:
    for key in (
        "language",
        "original_language",
        "audio_language",
        "default_audio_language",
    ):
        if language := _normalize_language_code(info.get(key)):
            return language

    channel = re.sub(
        r"\s+",
        " ",
        _first(info.get("channel") or info.get("uploader")),
    ).strip().casefold()
    if language := ORIGINAL_LANGUAGE_BY_CHANNEL.get(channel):
        return language

    title = _first(info.get("title"))
    if re.search(r"[\u3040-\u30ff]", title):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", title):
        return "ko"
    if re.search(r"[¿¡ñÑáéíóúüÁÉÍÓÚÜ]", title):
        return "es"
    return ""


def _subtitle_track_score(path: Path) -> tuple[int, int, int]:
    cues = _read_srt(path)
    duration = sum(max(0, cue.end_ms - cue.start_ms) for cue in cues)
    characters = sum(len(_clean_ass_text(cue.text)) for cue in cues)
    return duration, len(cues), characters


def _pick_source_subtitle(
    info: dict[str, Any],
    subtitles: dict[str, Path],
) -> tuple[str, Path] | None:
    declared_languages = [_inferred_original_language(info)]
    declared_languages = [language for language in declared_languages if language]
    by_lower = {
        language.casefold(): (language, path)
        for language, path in subtitles.items()
    }
    for language in declared_languages:
        base = _language_base(language)
        priorities = (
            f"{language}-orig",
            language,
            f"{base}-orig",
            base,
        )
        for candidate in priorities:
            match = by_lower.get(candidate.casefold())
            if match:
                return match
        prefixed = next(
            (
                (track_language, path)
                for track_language, path in subtitles.items()
                if _normalize_language_code(track_language) == language
                or (
                    base
                    and _language_base(track_language) == base
                    and track_language.casefold().endswith("-orig")
                )
            ),
            None,
        )
        if prefixed:
            return prefixed

    original_tracks = [
        (language, path)
        for language, path in subtitles.items()
        if language.casefold().endswith("-orig")
    ]
    non_translation_originals = [
        track
        for track in original_tracks
        if not _is_chinese_language(track[0])
    ]
    if non_translation_originals:
        return max(
            non_translation_originals,
            key=lambda track: _subtitle_track_score(track[1]),
        )

    foreign_tracks: dict[str, tuple[str, Path]] = {}
    for language, path in subtitles.items():
        normalized = _normalize_language_code(language)
        if (
            not normalized
            or _is_chinese_language(normalized)
            or _is_english_language(normalized)
        ):
            continue
        foreign_tracks.setdefault(_language_base(normalized), (language, path))
    if len(foreign_tracks) == 1:
        return next(iter(foreign_tracks.values()))
    return None


def _video_is_chinese(info: dict[str, Any], subtitle_languages: list[str]) -> bool:
    declared = _inferred_original_language(info)
    if declared:
        return _is_chinese_language(declared)

    original_tracks = [
        language[: -len("-orig")]
        for language in subtitle_languages
        if language.casefold().endswith("-orig")
    ]
    if original_tracks:
        return any(_is_chinese_language(language) for language in original_tracks)

    has_chinese = any(_is_chinese_language(language) for language in subtitle_languages)
    has_english = any(
        language.casefold() == "en" or language.casefold().startswith("en-")
        for language in subtitle_languages
    )
    return has_chinese and not has_english


def _comparable_subtitle_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", "", value).casefold()


def _subtitle_text_by_script(value: str, *, cjk: bool) -> str:
    lines = [_clean_ass_text(line) for line in value.splitlines()]
    selected = [
        line
        for line in lines
        if line and bool(CJK_TEXT_RE.search(line)) is cjk
    ]
    return _clean_ass_text(" ".join(selected))


def _normalize_chinese_caption_text(value: str) -> str:
    text = re.sub(r"\.{3,}|⋯+", "…", _clean_ass_text(value))
    output: list[str] = []
    inline_ascii_punctuation = {".", ",", ":", "/", "-", "_", "'", "’", "&", "@"}
    break_punctuation = {",", "，", ".", "。", "｡", "．", ";", "；"}
    paired_punctuation = set("“”‘’《》〈〉（）【】")
    for index, character in enumerate(text):
        if not unicodedata.category(character).startswith("P"):
            output.append(character)
            continue

        previous = text[index - 1] if index > 0 else ""
        following = text[index + 1] if index + 1 < len(text) else ""
        surrounded_by_ascii_tokens = (
            previous.isascii()
            and previous.isalnum()
            and following.isascii()
            and following.isalnum()
        )
        has_content_before = bool("".join(output).strip())
        has_content_after = bool(text[index + 1 :].strip())
        if character in break_punctuation and surrounded_by_ascii_tokens:
            output.append(character)
        elif character in break_punctuation:
            if has_content_before and has_content_after:
                output.append(" ")
        elif character in {"?", "？"}:
            if has_content_before and (not output or output[-1] != "？"):
                output.append("？")
        elif character in {"!", "！"}:
            if has_content_before and (not output or output[-1] != "！"):
                output.append("！")
        elif character == "、":
            if has_content_before and has_content_after:
                output.append(character)
        elif character in {":", "："}:
            if surrounded_by_ascii_tokens:
                output.append(":")
            elif has_content_before and has_content_after:
                output.append("：")
        elif character in {"…", "⋯"}:
            if not output or output[-1] != "…":
                output.append("…")
        elif character in paired_punctuation or character == "·":
            output.append(character)
        elif character == "-" and not has_content_before and has_content_after:
            output.append(character)
        elif character in inline_ascii_punctuation and surrounded_by_ascii_tokens:
            output.append(character)
        elif character == "#" and following.isascii() and following.isalnum():
            output.append(character)
        elif character in {"%", "％"} and previous.isdigit():
            output.append("%")
        elif surrounded_by_ascii_tokens:
            output.append(" ")

    normalized = "".join(output)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = re.sub(r"\s+([？！…：、”’》〉）】])", r"\1", normalized)
    normalized = re.sub(r"([“‘《〈（【])\s+", r"\1", normalized)
    return normalized


def _subtitle_quality_metrics(
    cues: list[BilingualSubtitleCue],
) -> dict[str, int]:
    metrics = {
        "short": 0,
        "chinese_long": 0,
        "secondary_long": 0,
        "chinese_fast": 0,
        "secondary_fast": 0,
    }
    for cue in cues:
        duration_ms = max(1, cue.end_ms - cue.start_ms)
        chinese_count = len(re.sub(r"\s+", "", cue.chinese))
        secondary_count = len(cue.secondary)
        metrics["short"] += duration_ms < BILINGUAL_MIN_DURATION_MS
        metrics["chinese_long"] += chinese_count > BILINGUAL_CHINESE_MAX_CHARS
        metrics["secondary_long"] += (
            secondary_count > BILINGUAL_ENGLISH_MAX_CHARS
        )
        metrics["chinese_fast"] += (
            chinese_count * 1_000 / duration_ms > BILINGUAL_CHINESE_MAX_CPS
        )
        metrics["secondary_fast"] += (
            secondary_count * 1_000 / duration_ms > BILINGUAL_ENGLISH_MAX_CPS
        )
    return metrics


def _closest_secondary_cue(
    chinese: SubtitleCue,
    secondary: list[SubtitleCue],
    start_index: int,
) -> tuple[SubtitleCue | None, int]:
    while (
        start_index + 1 < len(secondary)
        and secondary[start_index].end_ms < chinese.start_ms - 1_500
    ):
        start_index += 1

    best: SubtitleCue | None = None
    best_score: tuple[int, int] | None = None
    chinese_midpoint = (chinese.start_ms + chinese.end_ms) // 2
    for cue in secondary[max(0, start_index - 1) :]:
        if cue.start_ms > chinese.end_ms + 1_500:
            break
        overlap = max(
            0,
            min(chinese.end_ms, cue.end_ms) - max(chinese.start_ms, cue.start_ms),
        )
        midpoint_distance = abs(
            chinese_midpoint - ((cue.start_ms + cue.end_ms) // 2)
        )
        if overlap <= 0 and midpoint_distance > 1_500:
            continue
        score = (overlap, -midpoint_distance)
        if best_score is None or score > best_score:
            best = cue
            best_score = score
    return best, start_index


def _subtitle_text_for_language(value: str, language: str) -> str:
    if _is_english_language(language):
        return _subtitle_text_by_script(value, cjk=False)
    return _clean_ass_text(" ".join(value.splitlines()))


def _merge_bilingual_subtitles(
    secondary: list[SubtitleCue],
    chinese: list[SubtitleCue],
    secondary_language: str,
) -> list[BilingualSubtitleCue]:
    merged: list[BilingualSubtitleCue] = []
    secondary_index = 0
    for chinese_cue in chinese:
        secondary_cue, secondary_index = _closest_secondary_cue(
            chinese_cue,
            secondary,
            secondary_index,
        )
        chinese_text = _normalize_chinese_caption_text(
            _subtitle_text_by_script(chinese_cue.text, cjk=True)
        )
        secondary_text = ""
        if secondary_cue:
            secondary_text = _subtitle_text_for_language(
                secondary_cue.text,
                secondary_language,
            )
        if (
            _comparable_subtitle_text(secondary_text)
            == _comparable_subtitle_text(chinese_text)
        ):
            secondary_text = ""
        merged.append(
            BilingualSubtitleCue(
                start_ms=chinese_cue.start_ms,
                end_ms=chinese_cue.end_ms,
                secondary=secondary_text,
                chinese=chinese_text,
            )
        )
    return merged


def _create_bilingual_track(
    path: Path,
    secondary_language: str,
    secondary_cues: list[SubtitleCue],
    chinese_cues: list[SubtitleCue],
) -> dict[str, int]:
    bilingual = _merge_bilingual_subtitles(
        secondary_cues,
        chinese_cues,
        secondary_language,
    )
    synchronized, clipped, dropped = _synchronize_bilingual_cues(bilingual)
    _write_bilingual_ass(path, synchronized, secondary_language)
    quality = _subtitle_quality_metrics(synchronized)
    return {
        "cues": len(synchronized),
        "paired": sum(
            bool(cue.secondary and cue.chinese)
            for cue in synchronized
        ),
        "clipped": clipped,
        "dropped": dropped,
        **quality,
    }


def _chinese_subtitle_language(
    chinese: tuple[str, Path] | None,
) -> str:
    return "zh-TW" if chinese and chinese[0].casefold() in {
        "zh-hant",
        "zh-tw",
        "zh-hk",
    } else "zh-CN"


def _prepare_video_subtitles(
    info: dict[str, Any],
    temp_dir: Path,
    log_handle: Any,
) -> None:
    video_id = _first(info.get("id"))
    subtitle_paths = sorted(temp_dir.glob(f"{video_id}*.srt"))
    subtitles = {
        language: path
        for path in subtitle_paths
        if (language := _subtitle_language(path, video_id))
    }
    if not subtitles:
        log_handle.write(
            f"subtitles: {video_id} has no downloadable captions\n"
        )
        return

    chinese = _pick_subtitle(
        subtitles,
        ("zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "zh-HK"),
        "zh",
    )
    english = _pick_subtitle(subtitles, ("en-orig", "en"), "en")
    source = _pick_source_subtitle(info, subtitles)
    chinese_video = _video_is_chinese(info, list(subtitles))
    expected_source_language = _inferred_original_language(info)

    chinese_cues = _read_srt(chinese[1]) if chinese else []
    english_cues = _read_srt(english[1]) if english else []
    source_language = _normalize_language_code(source[0]) if source else ""
    source_cues = _read_srt(source[1]) if source else []
    for path in subtitle_paths:
        path.unlink(missing_ok=True)

    language = _chinese_subtitle_language(chinese)
    default_srt = temp_dir / f"{video_id}.{language}.default.srt"
    default_ass = temp_dir / f"{video_id}.{language}.default.ass"

    if chinese_video:
        if not chinese_cues:
            log_handle.write(
                f"subtitles: {video_id} is Chinese, but no Chinese caption track is available\n"
            )
            return
        _write_srt(default_srt, chinese_cues)
        log_handle.write(
            f"subtitles: Chinese video; kept one default {language} track "
            f"({len(chinese_cues)} cues)\n"
        )
        return

    if not chinese_cues:
        discarded = int(bool(english_cues)) + int(bool(source_cues))
        log_handle.write(
            f"subtitles: {video_id} has no usable Chinese captions; "
            f"discarded {discarded} foreign-only track(s)\n"
        )
        return

    secondary_language = ""
    secondary_cues: list[SubtitleCue] = []
    if (
        source_cues
        and source_language
        and not _is_chinese_language(source_language)
    ):
        secondary_language = source_language
        secondary_cues = source_cues
    elif english_cues and (
        not expected_source_language
        or _is_english_language(expected_source_language)
    ):
        secondary_language = "en"
        secondary_cues = english_cues

    if not secondary_cues:
        _write_srt(default_srt, chinese_cues)
        log_handle.write(
            f"subtitles: expected source language "
            f"{expected_source_language or 'unknown'} unavailable; kept one default "
            f"{language} track ({len(chinese_cues)} cues)\n"
        )
        return

    quality = _create_bilingual_track(
        default_ass,
        secondary_language,
        secondary_cues,
        chinese_cues,
    )
    log_handle.write(
        f"subtitles: created one default Chinese + {secondary_language} "
        f"bilingual {language} ASS track "
        f"({quality['paired']}/{quality['cues']} paired cues, "
        f"{quality['clipped']} overlaps clipped, "
        f"{quality['dropped']} invalid cues dropped); "
        "QC "
        f"short={quality['short']}, "
        f"zh>16={quality['chinese_long']}, "
        f"secondary>42={quality['secondary_long']}, "
        f"zh>9cps={quality['chinese_fast']}, "
        f"secondary>17cps={quality['secondary_fast']}\n"
    )


def _move_video(
    media_path: Path,
    info: dict[str, Any],
    temp_dir: Path,
    log_handle: Any,
) -> Path:
    _prepare_video_subtitles(info, temp_dir, log_handle)
    title = _safe_component(info.get("title"), "Untitled", limit=150)
    video_id = _safe_component(info.get("id"), "unknown", limit=32)
    uploader = _safe_component(info.get("uploader") or info.get("channel"), "Unknown Channel")
    playlist = _first(info.get("playlist"))
    if playlist.startswith("Uploads from"):
        playlist = ""
    collection = _safe_component(playlist, "单集")
    upload_date = _first(info.get("upload_date"))
    year = upload_date[:4] if len(upload_date) >= 4 else "未知年份"
    base_name = f"{title} [YT_{video_id}]"
    parent = VIDEO_ROOT / uploader / collection / year
    final_dir = parent / base_name
    if final_dir.exists():
        existing = next((path for path in final_dir.iterdir() if path.suffix.lower() in VIDEO_SUFFIXES), None)
        if existing:
            media_path.unlink(missing_ok=True)
            return existing
        final_dir = _unique_directory(final_dir)

    stage_dir = parent / f".{base_name}.partial-{secrets.token_hex(4)}"
    stage_dir.mkdir(parents=True, exist_ok=False)
    destination = stage_dir / f"{base_name}{media_path.suffix.lower()}"
    shutil.move(str(media_path), str(destination))

    source_thumbnail = _download_best_thumbnail(
        info,
        temp_dir,
        video_id,
        PROXY_URL,
        log_handle,
    )
    thumbnails = sorted(temp_dir.glob(f"{info.get('id')}*.jpg"))
    poster_name = "poster.jpg"
    selected_thumbnail = source_thumbnail or (thumbnails[0] if thumbnails else None)
    if selected_thumbnail:
        shutil.move(str(selected_thumbnail), str(stage_dir / poster_name))
        shutil.copy2(stage_dir / poster_name, stage_dir / "folder.jpg")
    else:
        poster_name = ""

    subtitle_paths = [
        *temp_dir.glob(f"{info.get('id')}*.srt"),
        *temp_dir.glob(f"{info.get('id')}*.ass"),
    ]
    for subtitle in sorted(subtitle_paths):
        suffix = subtitle.name[len(str(info.get("id"))) :]
        shutil.move(str(subtitle), str(stage_dir / f"{base_name}{suffix}"))

    nfo = _nfo_text(info, _first(info.get("title")) or title, video_id, poster_name)
    (stage_dir / f"{base_name}.nfo").write_text(nfo, encoding="utf-8")
    (stage_dir / "youtube.info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage_dir, final_dir)
    _set_output_permissions(final_dir, VIDEO_ROOT)
    return final_dir / destination.name


def _media_for_info(temp_dir: Path, video_id: str, kind: str) -> Path | None:
    candidates = [
        path
        for path in temp_dir.glob(f"{video_id}.*")
        if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES
    ]
    if kind == "youtube_video":
        candidates = [path for path in candidates if path.suffix.lower() in VIDEO_SUFFIXES]
    else:
        candidates = [path for path in candidates if path.suffix.lower() not in VIDEO_SUFFIXES]
    return max(candidates, key=lambda path: path.stat().st_size) if candidates else None


def _video_ids_from_probe(
    url: str,
    collection: bool,
    auth_strategy: str = "",
) -> list[str]:
    strategies = _ordered_auth_strategies(auth_strategy)
    if not strategies:
        return []
    command = [
        *_common_yt_dlp_args(strategies[0]),
        "--dump-json",
        "--flat-playlist",
        "--skip-download",
        "--playlist-end",
        str(PLAYLIST_MAX_ITEMS + 1),
    ]
    if not collection:
        command.append("--no-playlist")
    command.append(url)
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return []
    ids: list[str] = []
    for line in completed.stdout.splitlines():
        try:
            video_id = _first(json.loads(line).get("id"))
        except (json.JSONDecodeError, AttributeError):
            continue
        if video_id and video_id not in ids:
            ids.append(video_id)
    return ids


def _format_selection_args(kind: str) -> list[str]:
    if kind == "youtube_audio":
        return [
            "-f",
            "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio",
        ]
    height_filter = (
        f"[height<=?{VIDEO_MAX_HEIGHT}]"
        if VIDEO_MAX_HEIGHT
        else ""
    )
    adaptive_video_formats = f"bestvideo{height_filter}+bestaudio"
    return [
        "-f",
        adaptive_video_formats,
        "--format-sort-force",
        "--format-sort",
        VIDEO_FORMAT_SORT,
    ]


def _estimate_download_bytes(
    kind: str,
    url: str,
    collection: bool,
    strategy: YouTubeAuthStrategy,
) -> int:
    command = [
        *_common_yt_dlp_args(strategy),
        "--simulate",
        "--no-progress",
        "--print",
        "%(filesize,filesize_approx)s",
        *_format_selection_args(kind),
    ]
    if collection:
        command.extend(["--playlist-end", str(PLAYLIST_MAX_ITEMS)])
    else:
        command.append("--no-playlist")
    command.append(url)
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOWNLOAD_SIZE_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if completed.returncode != 0:
        return 0
    sizes: list[int] = []
    for line in completed.stdout.splitlines():
        try:
            size = int(line.strip())
        except ValueError:
            continue
        if size > 0:
            sizes.append(size)
    return sum(sizes)


def _download_proxy_for_size(estimated_bytes: int) -> str:
    if not LARGE_DOWNLOAD_PROXY_URL:
        return PROXY_URL
    if (
        estimated_bytes <= 0
        or estimated_bytes > LARGE_DOWNLOAD_THRESHOLD_BYTES
    ):
        return LARGE_DOWNLOAD_PROXY_URL
    return PROXY_URL


def _download_proxy_candidates(selected_proxy: str) -> list[str]:
    candidates = [selected_proxy]
    alternate_proxy = (
        PROXY_URL
        if selected_proxy == LARGE_DOWNLOAD_PROXY_URL
        else LARGE_DOWNLOAD_PROXY_URL
    )
    if alternate_proxy != selected_proxy:
        candidates.append(alternate_proxy)
    return candidates


def _download_proxy_label(proxy_url: str) -> str:
    if proxy_url == LARGE_DOWNLOAD_PROXY_URL:
        return "large-download LaP proxy"
    if proxy_url == PROXY_URL:
        return "default YouTube proxy"
    if proxy_url:
        return "alternate proxy"
    return "direct connection"


def _download_command(
    kind: str,
    url: str,
    temp_dir: Path,
    collection: bool,
    strategy: YouTubeAuthStrategy,
    proxy_url: str | None = None,
) -> list[str]:
    archive = prepare_attempt_archive(
        STATE_DIR / ("archive-audio.txt" if kind == "youtube_audio" else "archive-video.txt"),
        temp_dir,
    )
    command = [
        *_common_yt_dlp_args(strategy, proxy_url),
        "--newline",
        "--progress-template",
        (
            "download:"
            f"{YT_DLP_PROGRESS_PREFIX}"
            "%(progress.downloaded_bytes)s|"
            "%(progress.total_bytes)s|"
            "%(progress.total_bytes_estimate)s|"
            "%(progress.speed)s|"
            "%(progress.eta)s"
        ),
        "--concurrent-fragments",
        "4",
        "--sleep-requests",
        "0.5",
        "--sleep-interval",
        "1",
        "--max-sleep-interval",
        "3",
        "--download-archive",
        str(archive),
        "--write-info-json",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "--embed-metadata",
        "--trim-filenames",
        "180",
        "-o",
        str(temp_dir / "%(id)s.%(ext)s"),
    ]
    if collection:
        command.extend(["--playlist-end", str(PLAYLIST_MAX_ITEMS)])
    else:
        command.append("--no-playlist")
    if kind == "youtube_audio":
        command.extend(
            [
                *_format_selection_args(kind),
                "--extract-audio",
                "--audio-format",
                "m4a",
                "--audio-quality",
                "0",
                "--embed-thumbnail",
            ]
        )
    else:
        command.extend(
            [
                *_format_selection_args(kind),
                "--merge-output-format",
                VIDEO_MERGE_FORMAT,
            ]
        )
    command.append(url)
    return command


def _caption_key(
    tracks: dict[str, Any],
    priorities: tuple[str, ...],
    prefix: str = "",
) -> str:
    by_lower = {
        str(language).casefold(): str(language)
        for language in tracks
    }
    for language in priorities:
        match = by_lower.get(language.casefold())
        if match:
            return match
    prefix = prefix.casefold()
    if not prefix:
        return ""
    return next(
        (
            str(language)
            for language in tracks
            if str(language).casefold() == prefix
            or str(language).casefold().startswith(f"{prefix}-")
        ),
        "",
    )


def _source_caption_key(
    info: dict[str, Any],
    manual_tracks: dict[str, Any],
    automatic_tracks: dict[str, Any],
) -> tuple[str, str]:
    declared = [_inferred_original_language(info)]
    declared = [language for language in declared if language]
    manual_by_lower = {
        str(language).casefold(): str(language)
        for language in manual_tracks
    }
    automatic_by_lower = {
        str(language).casefold(): str(language)
        for language in automatic_tracks
    }
    for language in declared:
        base = _language_base(language)
        for candidate in (language, base):
            match = manual_by_lower.get(candidate.casefold())
            if match:
                return "manual", match
        for candidate in (
            f"{language}-orig",
            language,
            f"{base}-orig",
            base,
        ):
            match = automatic_by_lower.get(candidate.casefold())
            if match:
                return "automatic", match
        normalized_match = next(
            (
                str(track_language)
                for track_language in automatic_tracks
                if str(track_language).casefold().endswith("-orig")
                and _language_base(track_language) == base
            ),
            "",
        )
        if normalized_match:
            return "automatic", normalized_match

    original_tracks = [
        str(language)
        for language in automatic_tracks
        if str(language).casefold().endswith("-orig")
        and not _is_chinese_language(language)
    ]
    original_bases = {
        _language_base(language)
        for language in original_tracks
        if _language_base(language)
    }
    if len(original_bases) == 1 and original_tracks:
        return "automatic", original_tracks[0]
    return "", ""


def _desired_subtitle_downloads(temp_dir: Path) -> dict[str, list[str]]:
    desired = {
        "manual": [],
        "automatic": [],
    }
    found_info = False

    def add(kind: str, language: str) -> None:
        if language and language not in desired[kind]:
            desired[kind].append(language)

    for info_file in sorted(temp_dir.glob("*.info.json")):
        try:
            info = json.loads(info_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        found_info = True
        manual_tracks = (
            info.get("subtitles")
            if isinstance(info.get("subtitles"), dict)
            else {}
        )
        automatic_tracks = (
            info.get("automatic_captions")
            if isinstance(info.get("automatic_captions"), dict)
            else {}
        )

        chinese = _caption_key(
            manual_tracks,
            ("zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "zh-HK"),
            "zh",
        )
        if chinese:
            add("manual", chinese)
        else:
            add(
                "automatic",
                _caption_key(
                    automatic_tracks,
                    ("zh-Hans", "zh-CN", "zh", "zh-Hant", "zh-TW", "zh-HK"),
                    "zh",
                ),
            )

        source_kind, source_language = _source_caption_key(
            info,
            manual_tracks,
            automatic_tracks,
        )
        if source_kind:
            add(source_kind, source_language)
        else:
            expected = _inferred_original_language(info)
            if not expected or _is_english_language(expected):
                english = _caption_key(
                    manual_tracks,
                    ("en-orig", "en"),
                    "en",
                )
                if english:
                    add("manual", english)
                else:
                    add(
                        "automatic",
                        _caption_key(
                            automatic_tracks,
                            ("en-orig", "en"),
                            "en",
                        ),
                    )

    if not found_info:
        desired["automatic"] = [
            language.strip()
            for language in AUTOMATIC_SUBTITLE_LANGUAGES.split(",")
            if language.strip()
        ]
    return desired


def _download_video_subtitles(
    url: str,
    temp_dir: Path,
    collection: bool,
    strategy: YouTubeAuthStrategy,
    log_handle: Any,
) -> dict[str, bool]:
    desired = _desired_subtitle_downloads(temp_dir)
    subtitle_options = (
        (
            "manual",
            [
                "--write-subs",
                "--no-write-auto-subs",
                "--sub-langs",
                ",".join(desired["manual"]),
            ],
        ),
        (
            "automatic",
            [
                "--no-write-subs",
                "--write-auto-subs",
                "--sub-langs",
                ",".join(desired["automatic"]),
            ],
        ),
    )
    outcomes: dict[str, bool] = {}
    strategies = _ordered_auth_strategies(strategy.name) or [strategy]
    for label, options in subtitle_options:
        requested_languages = options[-1]
        if not requested_languages:
            outcomes[label] = True
            log_handle.write(
                f"subtitles: no selected {label} tracks are available\n"
            )
            log_handle.flush()
            continue
        completed_label = False
        for attempt_index, attempt_strategy in enumerate(strategies, 1):
            for rate_attempt in range(1, SUBTITLE_RATE_LIMIT_RETRIES + 1):
                command = [
                    *_common_yt_dlp_args(attempt_strategy),
                    "--skip-download",
                    "--newline",
                    "--no-progress",
                    "--sub-format",
                    "srt/best",
                    "--convert-subs",
                    "srt",
                    "--sleep-subtitles",
                    str(SUBTITLE_SLEEP_SECONDS),
                    "--no-overwrites",
                    "-o",
                    str(temp_dir / "%(id)s.%(ext)s"),
                ]
                if collection:
                    command.extend(["--playlist-end", str(PLAYLIST_MAX_ITEMS)])
                else:
                    command.append("--no-playlist")
                command.extend([*options, url])
                log_handle.write(
                    f"subtitles: fetching {label} tracks "
                    f"[{requested_languages}] with auth strategy "
                    f"{attempt_strategy.name} "
                    f"({attempt_index}/{len(strategies)}), rate attempt "
                    f"{rate_attempt}/{SUBTITLE_RATE_LIMIT_RETRIES}\n"
                )
                log_handle.flush()
                try:
                    completed = subprocess.run(
                        command,
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=SUBTITLE_DOWNLOAD_TIMEOUT_SECONDS,
                    )
                except subprocess.TimeoutExpired:
                    log_handle.write(
                        f"subtitles: {label} track fetch exceeded "
                        f"{SUBTITLE_DOWNLOAD_TIMEOUT_SECONDS} seconds\n"
                    )
                    log_handle.flush()
                    break
                if completed.stdout:
                    log_handle.write(completed.stdout)
                if completed.returncode == 0:
                    log_handle.write(
                        f"subtitles: {label} tracks fetched with "
                        f"{attempt_strategy.name}\n"
                    )
                    log_handle.flush()
                    completed_label = True
                    break
                rate_limited = "429" in (completed.stdout or "")
                if rate_limited and rate_attempt < SUBTITLE_RATE_LIMIT_RETRIES:
                    delay = SUBTITLE_RATE_LIMIT_RETRY_SECONDS * rate_attempt
                    log_handle.write(
                        f"subtitles: {label} tracks were rate limited; "
                        f"retrying the same auth strategy in {delay} seconds\n"
                    )
                    log_handle.flush()
                    time.sleep(delay)
                    continue
                log_handle.write(
                    f"subtitles: {label} track fetch exited with "
                    f"{completed.returncode}"
                    + (
                        "; trying the next auth strategy\n"
                        if attempt_index < len(strategies)
                        else "; video processing continues without missing tracks\n"
                    )
                )
                log_handle.flush()
                break
            if completed_label:
                break
        outcomes[label] = completed_label
    return outcomes


def _clear_download_attempt(temp_dir: Path) -> None:
    for path in temp_dir.iterdir():
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def _trigger_navidrome_scan() -> bool:
    if not NAVIDROME_URL or not NAVIDROME_USER or not NAVIDROME_PASSWORD:
        return False
    salt = secrets.token_hex(6)
    token = hashlib.md5((NAVIDROME_PASSWORD + salt).encode("utf-8")).hexdigest()
    query = urllib.parse.urlencode(
        {
            "u": NAVIDROME_USER,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "youtube-ingest",
            "f": "json",
        }
    )
    request = urllib.request.Request(f"{NAVIDROME_URL}/rest/startScan.view?{query}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return (payload.get("subsonic-response") or {}).get("status") == "ok"
    except Exception:
        return False


def trigger_navidrome_scan() -> bool:
    return _trigger_navidrome_scan()


def _emby_session() -> tuple[str, str]:
    if not EMBY_URL or not EMBY_USER or not EMBY_PASSWORD:
        return "", ""
    authorization = "MediaBrowser Client=YouTubeIngest, Device=NAS, DeviceId=youtube-ingest, Version=1.0"
    request = urllib.request.Request(
        f"{EMBY_URL}/Users/AuthenticateByName",
        data=json.dumps({"Username": EMBY_USER, "Pw": EMBY_PASSWORD}).encode("utf-8"),
        headers={"content-type": "application/json", "X-Emby-Authorization": authorization},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return (
            _first(payload.get("AccessToken")),
            _first((payload.get("User") or {}).get("Id")),
        )
    except Exception:
        return "", ""


def _emby_access_token() -> str:
    return _emby_session()[0]


def _emby_json(
    path: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 20,
) -> Any:
    data = None
    headers = {"X-Emby-Token": token}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json"
    request = urllib.request.Request(
        f"{EMBY_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body.decode("utf-8")) if body else None


def _emby_path(media_path: Path, local_root: Path, library_root: str) -> str:
    relative = media_path.resolve().relative_to(local_root.resolve())
    return f"{library_root.rstrip('/')}/{relative.as_posix()}"


def _emby_find_item(token: str, path: str, user_id: str = "") -> dict[str, Any] | None:
    query = urllib.parse.urlencode(
        {
            "Path": path,
            "Recursive": "true",
            "Fields": "Path,ExternalUrls,ImageTags",
        }
    )
    payload = _emby_json(f"/Items?{query}", token)
    for item in (payload or {}).get("Items") or []:
        if _first(item.get("Path")) == path:
            item_id = _first(item.get("Id"))
            if user_id and item_id:
                detail_query = urllib.parse.urlencode(
                    {"Fields": "Path,ProviderIds,ExternalUrls,ImageTags"}
                )
                try:
                    detail = _emby_json(
                        f"/Users/{user_id}/Items/{item_id}?{detail_query}",
                        token,
                    )
                except Exception:
                    detail = None
                if isinstance(detail, dict) and _first(detail.get("Path")) == path:
                    return detail
            return item
    return None


def _sync_emby_video_metadata(
    media_paths: list[str],
    local_root: Path,
    library_root: str,
    source_name: str,
    info_filename: str,
) -> int:
    if not EMBY_METADATA_SYNC_ENABLED or not media_paths:
        return 0
    token, user_id = _emby_session()
    if not token:
        return 0

    records: list[tuple[str, str]] = []
    for path_text in media_paths:
        media_path = Path(path_text)
        if not media_path.is_file():
            continue
        try:
            emby_path = _emby_path(media_path, local_root, library_root)
        except (OSError, ValueError):
            continue
        info_path = media_path.parent / info_filename
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = {}
        source_url = (
            _first(info.get("webpage_url"))
            or _first(info.get("original_url"))
            or _first(info.get("url"))
        )
        if not source_url:
            continue
        records.append((emby_path, source_url))

    if not records:
        return 0

    pending = list(records)
    verified: set[str] = set()
    deadline = time.monotonic() + EMBY_METADATA_SYNC_TIMEOUT_SECONDS
    while pending and time.monotonic() < deadline:
        next_pending: list[tuple[str, str]] = []
        for emby_path, source_url in pending:
            try:
                item = _emby_find_item(token, emby_path, user_id)
            except Exception:
                item = None
            links = (item or {}).get("ExternalUrls") or []
            has_source_link = any(
                isinstance(link, dict)
                and (
                    _first(link.get("Url")) == source_url
                    or _first(link.get("Name")) == f"{source_name} 视频源"
                )
                for link in links
            )
            has_primary_image = bool(((item or {}).get("ImageTags") or {}).get("Primary"))
            if item and has_source_link and has_primary_image:
                verified.add(emby_path)
            else:
                next_pending.append((emby_path, source_url))
        pending = next_pending
        if pending:
            time.sleep(3)
    return len(verified)


def _trigger_emby_scan() -> bool:
    token = _emby_access_token()
    if not token:
        return False
    request = urllib.request.Request(
        f"{EMBY_URL}/Library/Refresh",
        headers={"X-Emby-Token": token},
        data=b"",
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status in {200, 204}
    except Exception:
        return False


def trigger_emby_scan() -> bool:
    return _trigger_emby_scan()


def _download_once(
    kind: str,
    url: str,
    job_id: str,
    log_dir: Path,
    *,
    process_observer: ProcessObserver | None = None,
    progress_observer: ProgressObserver | None = None,
) -> DownloadResult:
    if not ENABLED:
        raise YouTubeIngestError("YouTube 下载功能尚未启用")
    if kind not in {"youtube_audio", "youtube_video"}:
        raise ValueError(f"unsupported YouTube job kind: {kind}")
    url = validate_url(url)
    probe = probe_url(url)
    _notify_progress(
        progress_observer,
        phase="读取媒体信息",
        title=probe.title,
    )
    if probe.is_collection and (probe.truncated or probe.item_count > PLAYLIST_MAX_ITEMS):
        raise YouTubeIngestError(
            f"播放列表/频道至少有 {probe.item_count} 项，超过安全上限 {PLAYLIST_MAX_ITEMS} 项"
        )

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    AUDIO_ROOT.mkdir(parents=True, exist_ok=True)
    VIDEO_ROOT.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "_", job_id)[:100]
    temp_dir = TEMP_ROOT / f"{int(time.time())}-{safe_job}-{secrets.token_hex(3)}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    log_file = log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe_job}-{kind}.log"
    tail: deque[str] = deque(maxlen=25)
    strategies = _ordered_auth_strategies(probe.auth_strategy)
    if not strategies:
        raise YouTubeIngestError("没有可用的 YouTube 认证策略")
    successful_strategy = strategies[0]
    try:
        with log_file.open("w", encoding="utf-8") as log_handle:
            if strategies[0].use_po_token:
                _ensure_po_token_provider()
            estimated_bytes = _estimate_download_bytes(
                kind,
                url,
                probe.is_collection,
                strategies[0],
            )
            selected_proxy = _download_proxy_for_size(estimated_bytes)
            if estimated_bytes > 0:
                _notify_progress(
                    progress_observer,
                    phase="准备下载",
                    title=probe.title,
                    total_bytes=estimated_bytes,
                    total_estimated=1,
                )
                log_handle.write(
                    "download size estimate: "
                    f"{estimated_bytes / 1024 / 1024:.1f} MiB; "
                    f"threshold={LARGE_DOWNLOAD_THRESHOLD_BYTES / 1024 / 1024:.1f} MiB; "
                    "route="
                    + (
                        "large-download LaP proxy\n"
                        if selected_proxy == LARGE_DOWNLOAD_PROXY_URL
                        else "default YouTube proxy\n"
                    )
                )
            else:
                log_handle.write(
                    "download size estimate unavailable; "
                    "using large-download LaP proxy as quota-safe fallback\n"
                )
            log_handle.flush()
            proxy_candidates = _download_proxy_candidates(selected_proxy)
            attempt_index = 0
            strategy_index = 0
            proxy_index = 0
            clear_partial_files = False
            highest_downloaded = 0
            highest_percent = 0.0
            while strategy_index < len(strategies):
                strategy = strategies[strategy_index]
                proxy_url = proxy_candidates[proxy_index]
                attempt_index += 1
                if strategy.use_po_token:
                    _ensure_po_token_provider()
                if clear_partial_files:
                    _clear_download_attempt(temp_dir)
                    clear_partial_files = False
                tail.clear()
                command = _download_command(
                    kind,
                    url,
                    temp_dir,
                    probe.is_collection,
                    strategy,
                    proxy_url,
                )
                redacted_command = [
                    "<cookies>" if arg == str(COOKIE_PATH) else arg
                    for arg in command
                ]
                log_handle.write(
                    f"\ndownload attempt {attempt_index}: "
                    f"auth={strategy.name}; "
                    f"route={_download_proxy_label(proxy_url)}\n"
                )
                log_handle.write("$ " + " ".join(redacted_command) + "\n")
                log_handle.flush()
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=True,
                )
                _notify_process(process_observer, process)
                assert process.stdout is not None
                timed_out = threading.Event()
                network_error_seen = False

                def terminate_download() -> None:
                    if process.poll() is None:
                        timed_out.set()
                        process.terminate()

                timer = threading.Timer(DOWNLOAD_TIMEOUT_SECONDS, terminate_download)
                timer.daemon = True
                timer.start()
                try:
                    for line in process.stdout:
                        log_handle.write(line)
                        log_handle.flush()
                        tail.append(line.rstrip())
                        progress = _parse_yt_dlp_progress(line)
                        if progress is not None:
                            downloaded = int(progress.get("downloaded_bytes") or 0)
                            highest_downloaded = max(highest_downloaded, downloaded)
                            if highest_downloaded:
                                progress["downloaded_bytes"] = highest_downloaded
                            if estimated_bytes > 0:
                                progress["total_bytes"] = estimated_bytes
                                progress["total_estimated"] = 1
                                progress["percent"] = min(
                                    99.5,
                                    highest_downloaded * 100 / estimated_bytes,
                                )
                            highest_percent = max(
                                highest_percent,
                                float(progress.get("percent") or 0.0),
                            )
                            progress["percent"] = highest_percent
                            progress["title"] = probe.title
                            _notify_progress(progress_observer, **progress)
                        if _is_retryable_youtube_network_error(line):
                            network_error_seen = True
                    return_code = process.wait()
                finally:
                    timer.cancel()
                    _notify_process(process_observer, None)
                if timed_out.is_set():
                    if process.poll() is None:
                        process.kill()
                    raise YouTubeIngestError(
                        f"YouTube 下载超过 {DOWNLOAD_TIMEOUT_SECONDS // 60} 分钟，已停止"
                    )
                if return_code == 0:
                    successful_strategy = strategy
                    log_handle.write(
                        f"auth strategy succeeded: {successful_strategy.name}\n"
                    )
                    break

                detail = "\n".join(list(tail)[-10:]) or "yt-dlp 没有输出错误详情"
                if (
                    (network_error_seen or _is_retryable_youtube_network_error(detail))
                    and proxy_index + 1 < len(proxy_candidates)
                ):
                    previous_proxy = proxy_url
                    proxy_index += 1
                    next_proxy = proxy_candidates[proxy_index]
                    log_handle.write(
                        "retryable network failure detected; preserving partial "
                        "files and switching route from "
                        f"{_download_proxy_label(previous_proxy)} to "
                        f"{_download_proxy_label(next_proxy)}\n"
                    )
                    log_handle.flush()
                    if AUTH_RETRY_DELAY_SECONDS:
                        time.sleep(AUTH_RETRY_DELAY_SECONDS)
                    continue
                if (
                    _is_retryable_youtube_error(detail)
                    and strategy_index + 1 < len(strategies)
                ):
                    strategy_index += 1
                    proxy_index = 0
                    clear_partial_files = True
                    log_handle.write(
                        "authentication/token failure detected; "
                        "cleaning partial files and trying the next strategy\n"
                    )
                    log_handle.flush()
                    if AUTH_RETRY_DELAY_SECONDS:
                        time.sleep(AUTH_RETRY_DELAY_SECONDS)
                    continue
                if _is_retryable_youtube_error(detail):
                    strategy_index = 0
                    proxy_index = 0
                    clear_partial_files = True
                    log_handle.write(
                        "all highest-quality strategies failed; lower-quality "
                        "fallback is disabled; restarting the highest-quality "
                        f"strategy cycle after {HIGH_QUALITY_RETRY_DELAY_SECONDS} "
                        "seconds\n"
                    )
                    log_handle.flush()
                    _notify_progress(
                        progress_observer,
                        phase="最高画质重试等待",
                        title=probe.title,
                        percent=highest_percent,
                        speed_bps=None,
                        eta=f"{HIGH_QUALITY_RETRY_DELAY_SECONDS}秒后重试",
                    )
                    time.sleep(HIGH_QUALITY_RETRY_DELAY_SECONDS)
                    continue
                raise YouTubeIngestError(
                    f"yt-dlp 下载失败，退出码 {return_code}\n{detail}"
                )

            _notify_progress(
                progress_observer,
                phase="整理媒体与元数据",
                title=probe.title,
                percent=max(highest_percent, 99.5),
                speed_bps=None,
                eta=None,
            )
            if kind == "youtube_video" and any(temp_dir.glob("*.info.json")):
                _download_video_subtitles(
                    url,
                    temp_dir,
                    probe.is_collection,
                    successful_strategy,
                    log_handle,
                )

            info_files = sorted(temp_dir.glob("*.info.json"))
            added: list[str] = []
            metadata_matches = 0
            lyrics_added = 0
            processed_ids: list[str] = []
            for info_file in info_files:
                try:
                    info = json.loads(info_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    log_handle.write(f"invalid info json {info_file}: {exc}\n")
                    continue
                video_id = _first(info.get("id"))
                if not video_id or video_id in processed_ids:
                    continue
                media_path = _media_for_info(temp_dir, video_id, kind)
                if media_path is None:
                    continue
                processed_ids.append(video_id)
                if kind == "youtube_audio":
                    destination, matched, has_lyrics = _move_audio(media_path, info, log_handle)
                    metadata_matches += int(matched)
                    lyrics_added += int(has_lyrics)
                else:
                    destination = _move_video(media_path, info, temp_dir, log_handle)
                    matched = False
                    has_lyrics = False
                added.append(str(destination))
                _record_download(
                    kind,
                    video_id,
                    _first(info.get("webpage_url")) or url,
                    _first(info.get("title")) or probe.title,
                    destination,
                    matched,
                    has_lyrics,
                )
                commit_imported_media(
                    STATE_DIR / ("archive-audio.txt" if kind == "youtube_audio" else "archive-video.txt"),
                    temp_dir / "download-archive.txt",
                    video_id,
                )

        all_ids = _video_ids_from_probe(
            url,
            probe.is_collection,
            successful_strategy.name,
        )
        existing = [path for path in _existing_downloads(kind, all_ids) if path not in added]
        if not added and not existing:
            raise YouTubeIngestError("下载命令已结束，但没有找到可入库的媒体文件")
        if kind == "youtube_audio":
            scan_triggered = _trigger_navidrome_scan()
            emby_metadata_synced = 0
        else:
            scan_triggered = _trigger_emby_scan()
            emby_metadata_synced = _sync_emby_video_metadata(
                [*added, *existing],
                VIDEO_ROOT,
                EMBY_YOUTUBE_LIBRARY_PATH,
                "YouTube",
                "youtube.info.json",
            )
        return DownloadResult(
            kind=kind,
            title=probe.title,
            added_paths=added,
            existing_paths=existing,
            metadata_matches=metadata_matches,
            lyrics_added=lyrics_added,
            log_file=str(log_file),
            scan_triggered=scan_triggered,
            emby_metadata_synced=emby_metadata_synced,
        )
    finally:
        _notify_process(process_observer, None)
        shutil.rmtree(temp_dir, ignore_errors=True)


def download(
    kind: str,
    url: str,
    job_id: str,
    log_dir: Path,
    *,
    process_observer: ProcessObserver | None = None,
    progress_observer: ProgressObserver | None = None,
) -> DownloadResult:
    try:
        return _download_once(
            kind,
            url,
            job_id,
            log_dir,
            process_observer=process_observer,
            progress_observer=progress_observer,
        )
    except YouTubeIngestError as exc:
        detail = str(exc)
        if (
            AUTH_AUTO_REFRESH_DISABLED
            or not _is_youtube_auth_error(detail)
            or "自动恢复结果" in detail
        ):
            raise
        refreshed, refresh_detail = _refresh_youtube_auth(validate_url(url))
        if refreshed:
            return _download_once(
                kind,
                url,
                job_id,
                log_dir,
                process_observer=process_observer,
                progress_observer=progress_observer,
            )
        raise YouTubeIngestError(
            f"{detail}\n自动恢复结果：{refresh_detail}"
        ) from exc


def cookie_status() -> str:
    names, missing = _cookie_health()
    if not names:
        return "未配置（公开内容仍可下载）"
    age_days = max(0, int((time.time() - COOKIE_PATH.stat().st_mtime) / 86400))
    if missing:
        return (
            f"已配置 {len(names)} 项，但登录链缺少 {len(missing)} 项："
            + ", ".join(sorted(missing))
        )
    provider = "PO Token 已启用" if PO_TOKEN_ENABLED else "PO Token 未启用"
    return f"完整 {len(names)} 项，{age_days} 天前更新，{provider}"
