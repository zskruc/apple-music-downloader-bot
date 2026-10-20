from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOT_ROOT = PROJECT_ROOT / "bot"
sys.path.insert(0, str(BOT_ROOT))

from task_cards import (
    TaskCardStore,
    delete_recorded_paths,
    format_task_card,
    task_keyboard,
)


class TaskCardTests(unittest.TestCase):
    def test_formats_live_card_and_buttons(self) -> None:
        task = {
            "id": 3,
            "kind": "youtube_video",
            "mode": "video",
            "status": "running",
            "phase": "下载中",
            "title": "示例视频",
            "percent": 37.5,
            "downloaded_bytes": 384 * 1024 * 1024,
            "total_bytes": 1024 * 1024 * 1024,
            "speed_bps": 8 * 1024 * 1024,
            "eta": "01:20",
            "created_at": 1000.0,
            "started_at": 1010.0,
            "finished_at": None,
            "error": "",
        }

        text = format_task_card(task, now=1040.0)

        self.assertIn("Apple Music Downloader 任务 #000003", text)
        self.assertIn("来源：YouTube", text)
        self.assertIn("██████░░░░░░░░░░ 37.5%", text)
        self.assertIn("当前速度：8.00 MiB/s", text)
        self.assertIn("预计剩余：01:20", text)
        self.assertEqual(
            task_keyboard(task),
            {
                "inline_keyboard": [
                    [
                        {"text": "暂停", "callback_data": "task:pause:3"},
                        {"text": "停止", "callback_data": "task:stop:3"},
                    ]
                ]
            },
        )

    def test_store_recovers_running_task_and_deduplicates_job_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TaskCardStore(Path(temporary) / "tasks.sqlite3")
            job = {
                "source": "telegram",
                "chat_id": "-1001",
                "message_id": "42",
                "urls": ["https://example.invalid/item"],
                "mode": "alac",
                "kind": "apple_music",
                "received_at": 1000.0,
            }
            task_id = store.create(
                job_key="telegram:-1001/42",
                job=job,
                source="telegram",
                chat_id="-1001",
                kind="apple_music",
                mode="alac",
                title="测试歌曲",
                created_at=1000.0,
            )
            store.update(task_id, status="running", phase="下载中", speed_bps=1024)

            duplicate = store.get_by_job_key("telegram:-1001/42")
            self.assertIsNotNone(duplicate)
            self.assertEqual(duplicate["id"], task_id)

            recovered = store.recover_after_restart()
            self.assertEqual([row["id"] for row in recovered], [task_id])
            self.assertEqual(recovered[0]["status"], "queued")
            self.assertEqual(recovered[0]["phase"], "容器重启后自动恢复")
            self.assertIsNone(recovered[0]["speed_bps"])
            self.assertEqual(store.job_from_task(recovered[0]), job)

    def test_formats_completion_card_and_delete_button(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            media = Path(temporary) / "示例歌曲.m4a"
            media.write_bytes(b"x" * 1536)
            task = {
                "id": 9,
                "kind": "apple_music",
                "mode": "alac",
                "status": "completed",
                "phase": "下载完成",
                "title": "示例歌曲",
                "created_at": 1_699_999_800.0,
                "started_at": 1_699_999_910.0,
                "finished_at": 1_700_000_000.0,
                "local_paths_json": json.dumps([str(media)], ensure_ascii=False),
                "delete_paths_json": json.dumps([str(media)], ensure_ascii=False),
            }

            text = format_task_card(task)

            self.assertIn("Apple Music Downloader 下载完成 #000009", text)
            self.assertIn("来源：Apple Music", text)
            self.assertIn("完成时间：2023-11-15 06:13:20", text)
            self.assertIn("总耗时：1分30秒", text)
            self.assertIn("• 示例歌曲.m4a（1.50 KiB）", text)
            self.assertIn("合计体积：1.50 KiB", text)
            self.assertIn(
                "下载根目录：/music/ALAC",
                text,
            )
            self.assertEqual(
                task_keyboard(task),
                {
                    "inline_keyboard": [
                        [
                            {
                                "text": "删除文件",
                                "callback_data": "task:delete:9",
                            }
                        ]
                    ]
                },
            )

    def test_store_persists_completion_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TaskCardStore(Path(temporary) / "tasks.sqlite3")
            task_id = store.create(
                job_key="telegram:-1001/88",
                job={"urls": ["https://example.invalid/song"]},
                source="telegram",
                chat_id="-1001",
                kind="apple_music",
                mode="alac",
                title="测试歌曲",
                created_at=1000.0,
            )
            paths = json.dumps(["/music/ALAC/album/song.m4a"])
            store.update(
                task_id,
                status="completed",
                local_paths_json=paths,
                delete_paths_json=paths,
            )

            task = store.get(task_id)

            self.assertIsNotNone(task)
            self.assertEqual(task["local_paths_json"], paths)
            self.assertEqual(task["delete_paths_json"], paths)

    def test_store_migrates_database_created_before_completion_cards(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "tasks.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE task_cards (
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
                        error TEXT
                    )
                    """
                )

            store = TaskCardStore(database)
            task_id = store.create(
                job_key="telegram:-1001/99",
                job={"urls": ["https://example.invalid/song"]},
                source="telegram",
                chat_id="-1001",
                kind="apple_music",
                mode="alac",
                title="旧数据库任务",
                created_at=1000.0,
            )
            task = store.get(task_id)

            self.assertIsNotNone(task)
            self.assertEqual(task["local_paths_json"], "[]")
            self.assertEqual(task["delete_paths_json"], "[]")

    def test_safe_delete_rejects_outside_paths_before_deleting_anything(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            allowed_root = base / "allowed"
            allowed_root.mkdir()
            inside = allowed_root / "inside.m4a"
            inside.write_bytes(b"inside")
            outside = base / "outside.m4a"
            outside.write_bytes(b"outside")

            deleted, rejected = delete_recorded_paths(
                [inside, outside],
                [allowed_root],
            )

            self.assertEqual(deleted, [])
            self.assertEqual(rejected, [str(outside)])
            self.assertTrue(inside.exists())
            self.assertTrue(outside.exists())

            deleted, rejected = delete_recorded_paths([inside], [allowed_root])
            self.assertEqual(deleted, ["inside.m4a"])
            self.assertEqual(rejected, [])
            self.assertFalse(inside.exists())
            self.assertTrue(outside.exists())

    def test_safe_delete_rejects_root_and_removes_recorded_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            allowed_root = Path(temporary) / "allowed"
            task_directory = allowed_root / "task-1"
            task_directory.mkdir(parents=True)
            (task_directory / "video.mp4").write_bytes(b"video")

            deleted, rejected = delete_recorded_paths(
                [allowed_root],
                [allowed_root],
            )
            self.assertEqual(deleted, [])
            self.assertEqual(rejected, [str(allowed_root)])
            self.assertTrue(task_directory.exists())

            deleted, rejected = delete_recorded_paths(
                [task_directory],
                [allowed_root],
            )
            self.assertEqual(deleted, ["task-1"])
            self.assertEqual(rejected, [])
            self.assertFalse(task_directory.exists())


if __name__ == "__main__":
    unittest.main()
