from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from typing import Any

import youtube_ingest as shared


BILIBILI_BASE_HOSTS = {"bilibili.com", "b23.tv", "bili2233.cn"}
BILIBILI_SHORT_HOSTS = {"b23.tv", "bili2233.cn"}
GENERIC_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,;:!?，。；：！？)]）】>\"'"
MEDIA_SUFFIXES = {".m4a", ".mp4", ".mkv", ".webm", ".opus", ".mp3"}
VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm"}

ENABLED = shared.env_flag("BILI_ENABLED", True)
AUTH_AUTO_REFRESH_DISABLED = shared.env_flag(
    "BILI_AUTH_AUTO_REFRESH_DISABLED",
    False,
)
STATE_DIR = Path(shared.env("BILI_STATE_DIR", "/state/bilibili"))
TEMP_ROOT = Path(shared.env("BILI_TEMP_PATH", "/state/bilibili/tmp"))
AUDIO_ROOT = Path(shared.env("BILI_AUDIO_OUTPUT_PATH", "/bilibili-music"))
VIDEO_ROOT = Path(shared.env("BILI_VIDEO_OUTPUT_PATH", "/bilibili-video"))
COOKIE_PATH = Path(shared.env("BILI_COOKIES_PATH", "/state/bilibili/cookies.txt"))
REQUIRED_COOKIE_NAMES = {
    name.strip()
    for name in shared.env(
        "BILI_REQUIRED_COOKIE_NAMES",
        "SESSDATA,bili_jct,DedeUserID,DedeUserID__ckMd5,buvid3,buvid4",
    ).split(",")
    if name.strip()
}
PROXY_URL = shared.env("BILI_DOWNLOAD_PROXY_URL", "http://host.docker.internal:7890")
PLAYLIST_MAX_ITEMS = max(1, int(shared.env("BILI_PLAYLIST_MAX_ITEMS", "50") or "50"))
VIDEO_MAX_HEIGHT = max(360, int(shared.env("BILI_VIDEO_MAX_HEIGHT", "2160") or "2160"))
EMBY_LIBRARY_PATH = shared.env("BILI_EMBY_LIBRARY_PATH", "/video/Bilibili")
PROBE_TIMEOUT_SECONDS = max(
    15,
    int(shared.env("BILI_PROBE_TIMEOUT_SECONDS", "60") or "60"),
)
DOWNLOAD_TIMEOUT_SECONDS = max(
    300,
    int(shared.env("BILI_DOWNLOAD_TIMEOUT_SECONDS", "21600") or "21600"),
)
DB_PATH = STATE_DIR / "downloads.sqlite3"


class BilibiliIngestError(RuntimeError):
    pass


def _host_allowed(host: str) -> bool:
    return any(host == base or host.endswith(f".{base}") for base in BILIBILI_BASE_HOSTS)


def _short_host(host: str) -> bool:
    return any(host == base or host.endswith(f".{base}") for base in BILIBILI_SHORT_HOSTS)


def validate_url(url: str) -> str:
    url = url.strip().rstrip(TRAILING_PUNCTUATION)
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not _host_allowed(host):
        raise ValueError("只允许哔哩哔哩的 http/https 链接")
    if parsed.username or parsed.password:
        raise ValueError("哔哩哔哩链接不能包含用户名或密码")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("哔哩哔哩链接使用了不允许的端口")

    path = parsed.path or "/"
    if not _short_host(host):
        allowed_prefixes = (
            "/video/",
            "/bangumi/play/",
            "/cheese/play/",
            "/medialist/play/",
            "/list/",
            "/festival/",
        )
        if not path.startswith(allowed_prefixes):
            raise ValueError("目前只接受哔哩哔哩视频、番剧单集或明确的媒体列表链接")

    tracking = {
        "spm_id_from",
        "vd_source",
        "share_source",
        "share_medium",
        "share_plat",
        "share_session_id",
        "share_tag",
        "timestamp",
        "unique_k",
        "bbid",
        "ts",
    }
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    clean_query = urllib.parse.urlencode(
        [(key, value) for key, value in query if key not in tracking]
    )
    return urllib.parse.urlunparse(
        ("https", parsed.netloc, path, parsed.params, clean_query, "")
    )


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in GENERIC_URL_RE.finditer(text):
        try:
            url = validate_url(match.group(0))
        except ValueError:
            continue
        if url not in urls:
            urls.append(url)
    return urls


def is_collection_hint(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/").lower()
    return path.startswith(("/medialist/play/", "/list/", "/festival/"))


def _cookie_names() -> set[str]:
    if not COOKIE_PATH.is_file() or COOKIE_PATH.stat().st_size == 0:
        return set()
    try:
        lines = COOKIE_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()

    names: set[str] = set()
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 7 and fields[5]:
            names.add(fields[5])
    return names


def _cookie_health() -> tuple[set[str], set[str]]:
    names = _cookie_names()
    return names, REQUIRED_COOKIE_NAMES - names


def _format_yt_dlp_error(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    detail = lines[-1] if lines else "未知错误"
    if "HTTP Error 412" not in stderr and "Precondition Failed" not in stderr:
        return detail

    names, missing = _cookie_health()
    if not missing:
        return (
            "B站拒绝了当前 NAS 登录会话（HTTP 412）。Cookie 可能已过期或"
            "触发风控，请启动 NAS 哔哩哔哩登录浏览器重新登录并导出"
        )
    if names:
        return (
            "B站拒绝了当前请求（HTTP 412），NAS Cookie 登录链不完整，缺少："
            + ", ".join(sorted(missing))
        )
    return (
        "B站拒绝了匿名请求（HTTP 412）。当前没有有效的登录 Cookie，"
        "请启动 NAS 哔哩哔哩登录浏览器完成登录并导出"
    )


AUTH_ERROR_MARKERS = (
    "http error 412",
    "precondition failed",
    "login_required",
    "login required",
    "authentication required",
    "sessdata",
    "账号未登录",
    "请先登录",
    "登录后",
    "触发风控",
    "risk control",
    "-101",
    "-10403",
)


def _is_bilibili_auth_error(detail: str) -> bool:
    normalized = detail.casefold()
    return any(marker in normalized for marker in AUTH_ERROR_MARKERS)


def _refresh_bilibili_auth(url: str) -> tuple[bool, str]:
    if AUTH_AUTO_REFRESH_DISABLED:
        return False, "本次 Cookie 候选校验不允许递归自动刷新"
    from auth_auto_refresh import refresh_auth

    result = refresh_auth("bilibili", url)
    return result.success, result.detail


def _common_yt_dlp_args() -> list[str]:
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
    if PROXY_URL:
        command.extend(["--proxy", PROXY_URL])
    _, missing = _cookie_health()
    if COOKIE_PATH.is_file() and COOKIE_PATH.stat().st_size > 0 and not missing:
        command.extend(["--cookies", str(COOKIE_PATH)])
    return command


def probe_url(
    url: str,
    _auto_refresh_attempted: bool = False,
) -> shared.ProbeResult:
    url = validate_url(url)
    collection = is_collection_hint(url)
    command = [
        *_common_yt_dlp_args(),
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
    except subprocess.TimeoutExpired as exc:
        raise BilibiliIngestError(
            f"读取哔哩哔哩链接超时（{PROBE_TIMEOUT_SECONDS} 秒）"
        ) from exc
    if completed.returncode != 0:
        detail = _format_yt_dlp_error(completed.stderr)
        if (
            _is_bilibili_auth_error(detail)
            and not _auto_refresh_attempted
            and not AUTH_AUTO_REFRESH_DISABLED
        ):
            refreshed, refresh_detail = _refresh_bilibili_auth(url)
            if refreshed:
                return probe_url(url, _auto_refresh_attempted=True)
            raise BilibiliIngestError(
                f"无法读取哔哩哔哩链接：{detail}。"
                f"自动恢复结果：{refresh_detail}"
            )
        raise BilibiliIngestError(
            f"无法读取哔哩哔哩链接：{detail}"
        )
    try:
        info = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BilibiliIngestError("yt-dlp 没有返回有效的链接信息") from exc

    entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
    reported_count = int(info.get("playlist_count") or info.get("n_entries") or 0)
    item_count = max(reported_count, len(entries), 1)
    truncated = item_count > PLAYLIST_MAX_ITEMS or len(entries) > PLAYLIST_MAX_ITEMS
    return shared.ProbeResult(
        url=url,
        title=str(info.get("title") or info.get("fulltitle") or "哔哩哔哩内容").strip(),
        uploader=str(info.get("uploader") or info.get("channel") or "").strip(),
        item_count=item_count,
        is_collection=bool(entries) or collection,
        truncated=truncated,
    )


def _database() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(DB_PATH)
    database.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            kind TEXT NOT NULL,
            media_id TEXT NOT NULL,
            source_url TEXT NOT NULL,
            title TEXT NOT NULL,
            output_path TEXT NOT NULL,
            metadata_matched INTEGER NOT NULL DEFAULT 0,
            lyrics_added INTEGER NOT NULL DEFAULT 0,
            downloaded_at REAL NOT NULL,
            PRIMARY KEY (kind, media_id)
        )
        """
    )
    database.commit()
    return database


def _existing_downloads(kind: str, media_ids: list[str]) -> list[str]:
    if not media_ids:
        return []
    placeholders = ",".join("?" for _ in media_ids)
    with _database() as database:
        rows = database.execute(
            f"SELECT output_path FROM downloads WHERE kind = ? AND media_id IN ({placeholders})",
            [kind, *media_ids],
        ).fetchall()
    return [str(path) for (path,) in rows if Path(str(path)).exists()]


def _record_download(
    kind: str,
    media_id: str,
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
                kind, media_id, source_url, title, output_path,
                metadata_matched, lyrics_added, downloaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, media_id) DO UPDATE SET
                source_url = excluded.source_url,
                title = excluded.title,
                output_path = excluded.output_path,
                metadata_matched = excluded.metadata_matched,
                lyrics_added = excluded.lyrics_added,
                downloaded_at = excluded.downloaded_at
            """,
            (
                kind,
                media_id,
                source_url,
                title,
                str(output_path),
                int(metadata_matched),
                int(lyrics_added),
                time.time(),
            ),
        )
        database.commit()


def _media_for_info(temp_dir: Path, media_id: str, kind: str) -> Path | None:
    candidates = [
        path
        for path in temp_dir.glob(f"{media_id}.*")
        if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES
    ]
    if kind == "bilibili_video":
        candidates = [path for path in candidates if path.suffix.lower() in VIDEO_SUFFIXES]
    else:
        candidates = [path for path in candidates if path.suffix.lower() not in VIDEO_SUFFIXES]
    return max(candidates, key=lambda path: path.stat().st_size) if candidates else None


def _video_ids_from_probe(url: str, collection: bool) -> list[str]:
    command = [
        *_common_yt_dlp_args(),
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
            media_id = shared._first(json.loads(line).get("id"))
        except (json.JSONDecodeError, AttributeError):
            continue
        if media_id and media_id not in ids:
            ids.append(media_id)
    return ids


def _download_command(kind: str, url: str, temp_dir: Path, collection: bool) -> list[str]:
    archive = shared.prepare_attempt_archive(
        STATE_DIR / ("archive-audio.txt" if kind == "bilibili_audio" else "archive-video.txt"),
        temp_dir,
    )
    command = [
        *_common_yt_dlp_args(),
        "--newline",
        "--progress-template",
        (
            "download:"
            f"{shared.YT_DLP_PROGRESS_PREFIX}"
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
        "--embed-chapters",
        "--trim-filenames",
        "180",
        "-o",
        str(temp_dir / "%(id)s.%(ext)s"),
    ]
    if collection:
        command.extend(["--playlist-end", str(PLAYLIST_MAX_ITEMS)])
    else:
        command.append("--no-playlist")
    if kind == "bilibili_audio":
        command.extend(
            [
                "-f",
                "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio",
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
                "-f",
                f"bestvideo*[height<={VIDEO_MAX_HEIGHT}][ext=mp4]+bestaudio[ext=m4a]/bestvideo*[height<={VIDEO_MAX_HEIGHT}]+bestaudio/best[height<={VIDEO_MAX_HEIGHT}]/best",
                "--merge-output-format",
                "mp4",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                "zh-Hans,zh-Hant,zh-CN,zh.*,ai-zh,en.*",
                "--sub-format",
                "srt/best",
                "--convert-subs",
                "srt",
            ]
        )
    command.append(url)
    return command


def _move_audio(
    media_path: Path,
    info: dict[str, Any],
    log_handle: Any,
) -> tuple[Path, bool, bool]:
    initial_metadata = shared._metadata_from_info(info)
    shared._write_initial_audio_tags(media_path, initial_metadata, info, "Bilibili")
    metadata_matched = shared._enrich_with_beets(media_path, log_handle)
    metadata = shared._final_audio_metadata(media_path, initial_metadata)

    artist = shared._safe_component(metadata["album_artist"], "Unknown Artist")
    album = shared._safe_component(metadata["album"], "Singles")
    title = shared._safe_component(metadata["title"], "Unknown Title", limit=150)
    media_id = shared._safe_component(info.get("id"), "unknown", limit=40)
    track = int(metadata["track_number"] or 0)
    filename = (
        f"{track:02d} - {title} [BILI_{media_id}]{media_path.suffix.lower()}"
        if track
        else f"{title} [BILI_{media_id}]{media_path.suffix.lower()}"
    )
    destination_dir = AUDIO_ROOT / artist / album
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / filename
    if destination.exists() and destination.stat().st_size == media_path.stat().st_size:
        media_path.unlink(missing_ok=True)
    else:
        destination = shared._unique_path(destination)
        shutil.move(str(media_path), str(destination))

    lyrics = shared._fetch_lrc(metadata)
    lyrics_added = False
    if lyrics:
        lyric_path = destination.with_suffix(".lrc")
        lyric_path.write_text(lyrics, encoding="utf-8")
        shared._set_output_permissions(lyric_path, AUDIO_ROOT)
        lyrics_added = True
    shared._set_output_permissions(destination, AUDIO_ROOT)
    return destination, metadata_matched, lyrics_added


def _source_provider_id(info: dict[str, Any], media_id: str) -> str | None:
    source_url = shared._first(info.get("webpage_url")) or shared._first(
        info.get("original_url")
    )
    if source_url:
        parsed = urllib.parse.urlparse(source_url)
        if _host_allowed((parsed.hostname or "").lower().rstrip(".")):
            match = re.search(r"(?i)(?:^|/)video/(BV[0-9A-Za-z]+|av\d+)", parsed.path)
            if match:
                return match.group(1)

    match = re.fullmatch(r"(?i)(BV[0-9A-Za-z]+)(?:_p(\d+))?", media_id)
    if match:
        return match.group(1)
    return None


def _nfo_text(info: dict[str, Any], title: str, media_id: str, poster_name: str) -> str:
    root = ET.Element("movie")
    ET.SubElement(root, "title").text = title
    ET.SubElement(root, "originaltitle").text = title
    ET.SubElement(root, "sorttitle").text = title
    ET.SubElement(root, "plot").text = shared._first(info.get("description"))
    upload_date = shared._first(info.get("upload_date"))
    if len(upload_date) == 8:
        premiered = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
        ET.SubElement(root, "premiered").text = premiered
        ET.SubElement(root, "year").text = upload_date[:4]
    duration = int(float(info.get("duration") or 0))
    ET.SubElement(root, "runtime").text = str(max(1, round(duration / 60))) if duration else "0"
    ET.SubElement(root, "studio").text = (
        shared._first(info.get("uploader")) or shared._first(info.get("channel"))
    )
    for genre in info.get("categories") or ["哔哩哔哩"]:
        ET.SubElement(root, "genre").text = shared._first(genre)
    for tag in (info.get("tags") or [])[:30]:
        ET.SubElement(root, "tag").text = shared._first(tag)
    provider_id = _source_provider_id(info, media_id)
    if provider_id:
        unique = ET.SubElement(root, "uniqueid", {"type": "bilibili", "default": "true"})
        unique.text = provider_id
    ET.SubElement(root, "thumb", {"aspect": "poster"}).text = poster_name
    ET.SubElement(root, "website").text = (
        shared._first(info.get("webpage_url"))
        or f"https://www.bilibili.com/video/{media_id}"
    )
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def _move_video(
    media_path: Path,
    info: dict[str, Any],
    temp_dir: Path,
    log_handle: Any,
) -> Path:
    title = shared._safe_component(info.get("title"), "Untitled", limit=150)
    media_id = shared._safe_component(info.get("id"), "unknown", limit=40)
    uploader = shared._safe_component(
        info.get("uploader") or info.get("channel"),
        "Unknown Uploader",
    )
    playlist = shared._first(info.get("playlist"))
    collection = shared._safe_component(playlist, "单集")
    upload_date = shared._first(info.get("upload_date"))
    year = upload_date[:4] if len(upload_date) >= 4 else "未知年份"
    base_name = f"{title} [BILI_{media_id}]"
    parent = VIDEO_ROOT / uploader / collection / year
    final_dir = parent / base_name
    if final_dir.exists():
        existing = next(
            (path for path in final_dir.iterdir() if path.suffix.lower() in VIDEO_SUFFIXES),
            None,
        )
        if existing:
            media_path.unlink(missing_ok=True)
            return existing
        final_dir = shared._unique_directory(final_dir)

    stage_dir = parent / f".{base_name}.partial-{secrets.token_hex(4)}"
    stage_dir.mkdir(parents=True, exist_ok=False)
    destination = stage_dir / f"{base_name}{media_path.suffix.lower()}"
    shutil.move(str(media_path), str(destination))

    source_thumbnail = shared._download_best_thumbnail(
        info,
        temp_dir,
        media_id,
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

    for subtitle in sorted(temp_dir.glob(f"{info.get('id')}*.srt")):
        suffix = subtitle.name[len(str(info.get("id"))) :]
        shutil.move(str(subtitle), str(stage_dir / f"{base_name}{suffix}"))

    nfo = _nfo_text(info, shared._first(info.get("title")) or title, media_id, poster_name)
    (stage_dir / f"{base_name}.nfo").write_text(nfo, encoding="utf-8")
    (stage_dir / "bilibili.info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    parent.mkdir(parents=True, exist_ok=True)
    os.replace(stage_dir, final_dir)
    shared._set_output_permissions(final_dir, VIDEO_ROOT)
    return final_dir / destination.name


def _download_once(
    kind: str,
    url: str,
    job_id: str,
    log_dir: Path,
    *,
    process_observer: shared.ProcessObserver | None = None,
    progress_observer: shared.ProgressObserver | None = None,
) -> shared.DownloadResult:
    if not ENABLED:
        raise BilibiliIngestError("哔哩哔哩下载功能尚未启用")
    if kind not in {"bilibili_audio", "bilibili_video"}:
        raise ValueError(f"unsupported Bilibili job kind: {kind}")
    url = validate_url(url)
    probe = probe_url(url)
    shared._notify_progress(
        progress_observer,
        phase="读取媒体信息",
        title=probe.title,
    )
    if probe.is_collection and (probe.truncated or probe.item_count > PLAYLIST_MAX_ITEMS):
        raise BilibiliIngestError(
            f"媒体列表至少有 {probe.item_count} 项，超过安全上限 {PLAYLIST_MAX_ITEMS} 项"
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
    command = _download_command(kind, url, temp_dir, probe.is_collection)
    tail: deque[str] = deque(maxlen=25)
    try:
        with log_file.open("w", encoding="utf-8") as log_handle:
            redacted_command = ["<cookies>" if arg == str(COOKIE_PATH) else arg for arg in command]
            log_handle.write("$ " + " ".join(redacted_command) + "\n")
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
            shared._notify_process(process_observer, process)
            assert process.stdout is not None
            timed_out = threading.Event()
            highest_downloaded = 0
            highest_percent = 0.0

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
                    progress = shared._parse_yt_dlp_progress(line)
                    if progress is not None:
                        downloaded = int(progress.get("downloaded_bytes") or 0)
                        highest_downloaded = max(highest_downloaded, downloaded)
                        if highest_downloaded:
                            progress["downloaded_bytes"] = highest_downloaded
                        highest_percent = max(
                            highest_percent,
                            float(progress.get("percent") or 0.0),
                        )
                        progress["percent"] = highest_percent
                        progress["title"] = probe.title
                        shared._notify_progress(progress_observer, **progress)
                return_code = process.wait()
            finally:
                timer.cancel()
                shared._notify_process(process_observer, None)
            if timed_out.is_set():
                if process.poll() is None:
                    process.kill()
                raise BilibiliIngestError(
                    f"哔哩哔哩下载超过 {DOWNLOAD_TIMEOUT_SECONDS // 60} 分钟，已停止"
                )
            if return_code != 0:
                detail = "\n".join(list(tail)[-10:]) or "yt-dlp 没有输出错误详情"
                raise BilibiliIngestError(
                    f"yt-dlp 下载失败，退出码 {return_code}\n{detail}"
                )

            shared._notify_progress(
                progress_observer,
                phase="整理媒体与元数据",
                title=probe.title,
                percent=max(highest_percent, 99.5),
                speed_bps=None,
                eta=None,
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
                media_id = shared._first(info.get("id"))
                if not media_id or media_id in processed_ids:
                    continue
                media_path = _media_for_info(temp_dir, media_id, kind)
                if media_path is None:
                    continue
                processed_ids.append(media_id)
                if kind == "bilibili_audio":
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
                    media_id,
                    shared._first(info.get("webpage_url")) or url,
                    shared._first(info.get("title")) or probe.title,
                    destination,
                    matched,
                    has_lyrics,
                )
                shared.commit_imported_media(
                    STATE_DIR / ("archive-audio.txt" if kind == "bilibili_audio" else "archive-video.txt"),
                    temp_dir / "download-archive.txt",
                    media_id,
                )

        all_ids = _video_ids_from_probe(url, probe.is_collection)
        existing = [path for path in _existing_downloads(kind, all_ids) if path not in added]
        if not added and not existing:
            raise BilibiliIngestError("下载命令已结束，但没有找到可入库的媒体文件")
        if kind == "bilibili_audio":
            scan_triggered = shared._trigger_navidrome_scan()
            emby_metadata_synced = 0
        else:
            scan_triggered = shared._trigger_emby_scan()
            emby_metadata_synced = shared._sync_emby_video_metadata(
                [*added, *existing],
                VIDEO_ROOT,
                EMBY_LIBRARY_PATH,
                "哔哩哔哩",
                "bilibili.info.json",
            )
        return shared.DownloadResult(
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
        shared._notify_process(process_observer, None)
        shutil.rmtree(temp_dir, ignore_errors=True)


def download(
    kind: str,
    url: str,
    job_id: str,
    log_dir: Path,
    *,
    process_observer: shared.ProcessObserver | None = None,
    progress_observer: shared.ProgressObserver | None = None,
) -> shared.DownloadResult:
    try:
        return _download_once(
            kind,
            url,
            job_id,
            log_dir,
            process_observer=process_observer,
            progress_observer=progress_observer,
        )
    except BilibiliIngestError as exc:
        detail = str(exc)
        if (
            AUTH_AUTO_REFRESH_DISABLED
            or not _is_bilibili_auth_error(detail)
            or "自动恢复结果" in detail
        ):
            raise
        refreshed, refresh_detail = _refresh_bilibili_auth(validate_url(url))
        if refreshed:
            return _download_once(
                kind,
                url,
                job_id,
                log_dir,
                process_observer=process_observer,
                progress_observer=progress_observer,
            )
        raise BilibiliIngestError(
            f"{detail}\n自动恢复结果：{refresh_detail}"
        ) from exc


def cookie_status() -> str:
    if not COOKIE_PATH.exists() or COOKIE_PATH.stat().st_size == 0:
        return "未配置（部分公开内容会被 B 站以 HTTP 412 拦截）"
    age_days = max(0, int((time.time() - COOKIE_PATH.stat().st_mtime) / 86400))
    names, missing = _cookie_health()
    if missing:
        return (
            f"已配置 {len(names)} 项，但登录链缺少 {len(missing)} 项："
            + ", ".join(sorted(missing))
        )
    return f"完整 {len(names)} 项，{age_days} 天前更新"
