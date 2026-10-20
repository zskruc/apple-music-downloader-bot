from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


ACTIVE_STATUSES = {"queued", "running", "paused", "stopping"}
TERMINAL_STATUSES = {"completed", "failed", "stopped", "deleted"}

STATUS_LABELS = {
    "queued": "排队中",
    "running": "正在下载",
    "paused": "已暂停",
    "stopping": "正在停止",
    "completed": "已完成",
    "failed": "失败",
    "stopped": "已停止",
    "deleted": "文件已删除",
}

SOURCE_LABELS = {
    "apple_music": "Apple Music",
    "lx_music": "LX Music 高品质音源",
    "youtube_audio": "YouTube Music",
    "youtube_video": "YouTube",
    "bilibili_audio": "哔哩哔哩音乐",
    "bilibili_video": "哔哩哔哩视频",
}

SAVE_DIRECTORIES = {
    "apple_music:alac": "/music/ALAC",
    "apple_music:atmos": "/music/Atmos",
    "apple_music:both": "/music",
    "lx_music": "/music/LX Music",
    "youtube_audio": "/youtube-music",
    "youtube_video": "/youtube-video",
    "bilibili_audio": "/bilibili-music",
    "bilibili_video": "/bilibili-video",
}

CONTAINER_DISPLAY_ROOTS = {
    "/music": "/music",
    "/youtube-music": "/youtube-music",
    "/youtube-video": "/youtube-video",
    "/bilibili-music": "/bilibili-music",
    "/bilibili-video": "/bilibili-video",
}
DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")


def format_size(value: float | int | None) -> str:
    if value is None:
        return "等待统计"
    size = max(0.0, float(value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


def format_duration(seconds: float | int | None) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分{seconds}秒"
    if minutes:
        return f"{minutes}分{seconds}秒"
    return f"{seconds}秒"


def progress_bar(percent: float, width: int = 16) -> str:
    bounded = min(100.0, max(0.0, percent))
    filled = round(width * bounded / 100.0)
    return "█" * filled + "░" * (width - filled)


def truncate(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def json_paths(value: object) -> list[Path]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(decoded, list):
        return []
    paths: list[Path] = []
    for item in decoded:
        text = str(item or "").strip()
        if text:
            paths.append(Path(text))
    return paths


def path_size(path: Path) -> int:
    try:
        if path.is_file() or path.is_symlink():
            return path.stat().st_size
        if path.is_dir():
            return sum(
                candidate.stat().st_size
                for candidate in path.rglob("*")
                if candidate.is_file()
            )
    except OSError:
        return 0
    return 0


def display_media_path(path: Path) -> str:
    text = str(path)
    for container_root, display_root in CONTAINER_DISPLAY_ROOTS.items():
        if text == container_root:
            return display_root
        prefix = container_root.rstrip("/") + "/"
        if text.startswith(prefix):
            return display_root.rstrip("/") + "/" + text[len(prefix) :]
    return text


def is_unified_task(task: dict[str, Any]) -> bool:
    try:
        job = json.loads(str(task.get("job_json") or "{}"))
        payload = job.get("payload")
        return (str(task.get("kind")) == "apple_music" and str(task.get("mode")) == "alac"
                and isinstance(payload, dict) and "unified_music" in payload)
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False


def task_source_label(task: dict[str, Any]) -> str:
    if is_unified_task(task):
        if str(task.get("status")) == "completed":
            phase = str(task.get("phase") or "")
            for provider, label in (("SQ ", "SQ Music 无损"), ("LX ", "LX Music 无损"), ("Apple Music ", "Apple Music ALAC")):
                if phase.startswith(provider):
                    return label
        return "统一无损（Apple → LX → SQ）"
    kind = str(task["kind"])
    return SOURCE_LABELS.get(kind, kind)


def completion_root(task: dict[str, Any]) -> str:
    if is_unified_task(task):
        paths = json_paths(task.get("local_paths_json"))
        for root in ("/music/SQ Music", "/music/LX Music", "/music/ALAC"):
            if paths and all(path.is_relative_to(root) for path in paths):
                return display_media_path(Path(root))
        return display_media_path(Path("/music"))
    kind = str(task["kind"])
    mode = str(task["mode"])
    save_key = f"{kind}:{mode}" if kind == "apple_music" else kind
    return SAVE_DIRECTORIES.get(save_key, "/downloads")


def format_completion_card(task: dict[str, Any]) -> str:
    task_id = int(task["id"])
    kind = str(task["kind"])
    finished_at = float(task.get("finished_at") or time.time())
    started_at = float(task.get("started_at") or task.get("created_at") or finished_at)
    paths = json_paths(task.get("local_paths_json"))
    lines = [
        f"Apple Music Downloader 下载完成 #{task_id:06d}",
        f"来源：{task_source_label(task)}",
        (
            "完成时间："
            + datetime.fromtimestamp(finished_at, DISPLAY_TIMEZONE).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        ),
        f"总耗时：{format_duration(finished_at - started_at)}",
        "文件清单：",
    ]
    total_size = 0
    if paths:
        for path in paths[:12]:
            size = path_size(path)
            total_size += size
            lines.append(f"• {truncate(path.name, 120)}（{format_size(size)}）")
            lines.append(f"  {truncate(display_media_path(path), 280)}")
        if len(paths) > 12:
            remaining = paths[12:]
            total_size += sum(path_size(path) for path in remaining)
            lines.append(f"• 另有 {len(remaining)} 项")
    else:
        lines.append(f"• {truncate(task.get('title') or '任务已完成', 180)}")
    lines.extend(
        [
            f"合计体积：{format_size(total_size)}",
            f"下载根目录：{completion_root(task)}",
        ]
    )
    return "\n".join(lines)


def format_task_card(task: dict[str, Any], now: float | None = None) -> str:
    now = time.time() if now is None else now
    task_id = int(task["id"])
    status = str(task["status"])
    if status == "completed":
        return format_completion_card(task)
    kind = str(task["kind"])
    mode = str(task["mode"])
    title = truncate(task.get("title") or "等待读取媒体信息", 110)
    percent = min(100.0, max(0.0, float(task.get("percent") or 0)))
    downloaded_bytes = task.get("downloaded_bytes")
    total_bytes = task.get("total_bytes")
    speed_bps = task.get("speed_bps")
    started_at = task.get("started_at") or task.get("created_at") or now
    finished_at = task.get("finished_at")
    end = float(finished_at or now)
    source = task_source_label(task)
    save_key = f"{kind}:{mode}" if kind == "apple_music" else kind
    save_directory = completion_root(task) if is_unified_task(task) else SAVE_DIRECTORIES.get(save_key, "/downloads")

    lines = [
        f"Apple Music Downloader 任务 #{task_id:06d}",
        f"来源：{source}",
        f"状态：{STATUS_LABELS.get(status, status)}",
        f"阶段：{truncate(task.get('phase') or '等待开始', 80)}",
        f"内容：{title}",
        f"{progress_bar(percent)} {percent:.1f}%",
        (
            f"已下载：{format_size(downloaded_bytes)} / "
            f"{format_size(total_bytes)}"
        ),
        (
            f"当前速度：{format_size(speed_bps)}/s"
            if speed_bps is not None and float(speed_bps) > 0
            else "当前速度：等待数据"
        ),
    ]
    eta = str(task.get("eta") or "").strip()
    if eta and eta.casefold() != "unknown":
        lines.append(f"预计剩余：{eta}")
    else:
        lines.append("预计剩余：等待统计")
    lines.append(f"耗时：{format_duration(end - float(started_at))}")
    lines.append(f"保存目录：{save_directory}")
    error = truncate(task.get("error") or "", 700)
    if error:
        lines.append(f"说明：{error}")
    return "\n".join(lines)


def task_keyboard(task: dict[str, Any]) -> dict[str, Any] | None:
    task_id = int(task["id"])
    status = str(task["status"])
    if status in {"queued", "running"}:
        if str(task.get("kind") or "") == "lx_music":
            buttons = [
                {"text": "停止", "callback_data": f"task:stop:{task_id}"},
            ]
        else:
            buttons = [
                {"text": "暂停", "callback_data": f"task:pause:{task_id}"},
                {"text": "停止", "callback_data": f"task:stop:{task_id}"},
            ]
    elif status == "paused":
        buttons = [
            {"text": "继续", "callback_data": f"task:resume:{task_id}"},
            {"text": "停止", "callback_data": f"task:stop:{task_id}"},
        ]
    elif status == "completed" and json_paths(task.get("delete_paths_json")):
        buttons = [
            {"text": "删除文件", "callback_data": f"task:delete:{task_id}"},
        ]
    else:
        return None
    return {"inline_keyboard": [buttons]}


def is_path_within(path: Path, root: Path) -> bool:
    try:
        resolved_path = path.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
        relative = resolved_path.relative_to(resolved_root)
    except (OSError, ValueError):
        return False
    return bool(relative.parts)


def delete_recorded_paths(
    paths: Iterable[Path],
    allowed_roots: Iterable[Path],
) -> tuple[list[str], list[str]]:
    roots = list(allowed_roots)
    deleted: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()
    candidates: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not any(is_path_within(path, root) for root in roots):
            rejected.append(key)
            continue
        candidates.append(path)
    if rejected:
        return [], rejected
    for path in candidates:
        if not path.exists() and not path.is_symlink():
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted.append(path.name)
        except OSError as exc:
            rejected.append(f"{path.name}: {exc}")
    return deleted, rejected


class TaskCardStore:
    _UPDATABLE_COLUMNS = {
        "status_message_id",
        "status",
        "phase",
        "title",
        "started_at",
        "finished_at",
        "percent",
        "downloaded_bytes",
        "total_bytes",
        "total_estimated",
        "speed_bps",
        "eta",
        "error",
        "local_paths_json",
        "delete_paths_json",
    }

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_key TEXT NOT NULL UNIQUE,
                    job_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    status_message_id TEXT,
                    kind TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    phase TEXT NOT NULL DEFAULT '等待开始',
                    title TEXT,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    percent REAL NOT NULL DEFAULT 0,
                    downloaded_bytes INTEGER,
                    total_bytes INTEGER,
                    total_estimated INTEGER NOT NULL DEFAULT 0,
                    speed_bps REAL,
                    eta TEXT,
                    error TEXT,
                    local_paths_json TEXT NOT NULL DEFAULT '[]',
                    delete_paths_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS task_cards_status_id
                    ON task_cards(status, id);
                """
            )
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    "PRAGMA table_info(task_cards)"
                ).fetchall()
            }
            if "local_paths_json" not in columns:
                self._connection.execute(
                    "ALTER TABLE task_cards ADD COLUMN "
                    "local_paths_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "delete_paths_json" not in columns:
                self._connection.execute(
                    "ALTER TABLE task_cards ADD COLUMN "
                    "delete_paths_json TEXT NOT NULL DEFAULT '[]'"
                )

    def create(
        self,
        *,
        job_key: str,
        job: dict[str, Any],
        source: str,
        chat_id: str,
        kind: str,
        mode: str,
        title: str,
        created_at: float,
    ) -> int:
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT INTO task_cards (
                    job_key, job_json, source, chat_id, kind, mode,
                    title, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_key,
                    json.dumps(job, ensure_ascii=False),
                    source,
                    chat_id,
                    kind,
                    mode,
                    title,
                    created_at,
                ),
            )
            return int(cursor.lastrowid)

    def get_by_job_key(self, job_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM task_cards WHERE job_key = ?",
                (job_key,),
            ).fetchone()
        return dict(row) if row else None

    def get(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM task_cards WHERE id = ?",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def update(self, task_id: int, **values: Any) -> None:
        invalid = set(values) - self._UPDATABLE_COLUMNS
        if invalid:
            raise ValueError(f"unsupported task card columns: {sorted(invalid)}")
        if not values:
            return
        assignments = ", ".join(f"{column} = ?" for column in values)
        parameters = [*values.values(), task_id]
        with self._lock:
            self._connection.execute(
                f"UPDATE task_cards SET {assignments} WHERE id = ?",
                parameters,
            )

    def list_statuses(self, statuses: Iterable[str]) -> list[dict[str, Any]]:
        status_list = list(statuses)
        if not status_list:
            return []
        placeholders = ",".join("?" for _ in status_list)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT * FROM task_cards
                WHERE status IN ({placeholders})
                ORDER BY id
                """,
                status_list,
            ).fetchall()
        return [dict(row) for row in rows]

    def recover_after_restart(self) -> list[dict[str, Any]]:
        with self._lock:
            self._connection.execute(
                """
                UPDATE task_cards
                SET status = 'queued',
                    phase = '容器重启后自动恢复',
                    speed_bps = NULL,
                    eta = NULL
                WHERE status IN ('running', 'stopping')
                """
            )
        return self.list_statuses({"queued", "paused"})

    @staticmethod
    def job_from_task(task: dict[str, Any]) -> dict[str, Any]:
        return json.loads(str(task["job_json"]))
