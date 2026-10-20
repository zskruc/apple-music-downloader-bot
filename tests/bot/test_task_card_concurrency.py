from __future__ import annotations

import importlib
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOT_ROOT = PROJECT_ROOT / "bot"
sys.path.insert(0, str(BOT_ROOT))

IMPORT_STATE = tempfile.TemporaryDirectory()
os.environ.setdefault("AMDL_STATE_DIR", str(Path(IMPORT_STATE.name) / "state"))
os.environ.setdefault("AMDL_LOG_DIR", str(Path(IMPORT_STATE.name) / "logs"))
os.environ.setdefault("AMDL_TEMP_PATH", str(Path(IMPORT_STATE.name) / "tmp"))
os.environ.setdefault("YT_STATE_DIR", str(Path(IMPORT_STATE.name) / "youtube"))
os.environ.setdefault("BILI_STATE_DIR", str(Path(IMPORT_STATE.name) / "bilibili"))

mutagen = types.ModuleType("mutagen")
mutagen.File = lambda *_args, **_kwargs: None
mutagen_mp4 = types.ModuleType("mutagen.mp4")
mutagen_mp4.MP4 = object
sys.modules.setdefault("mutagen", mutagen)
sys.modules.setdefault("mutagen.mp4", mutagen_mp4)

ingest_bot = importlib.import_module("ingest_bot")
TaskCardStore = importlib.import_module("task_cards").TaskCardStore


class TaskCardConcurrencyTests(unittest.TestCase):
    def test_simultaneous_first_updates_send_only_one_card(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = TaskCardStore(Path(temporary) / "tasks.sqlite3")
            task_id = store.create(
                job_key="telegram:-1001/race",
                job={"urls": ["https://example.invalid/item"]},
                source="telegram",
                chat_id="-1001",
                kind="youtube_video",
                mode="video",
                title="并发测试",
                created_at=1000.0,
            )
            send_started = threading.Event()
            event_lock = threading.Lock()
            sent: list[str] = []
            edited: list[str] = []
            errors: list[BaseException] = []
            results: list[bool] = []

            def fake_send(
                _chat_id: str,
                _text: str,
                _reply_markup: dict[str, object] | None = None,
                _parse_mode_name: str | None = None,
            ) -> dict[str, int]:
                with event_lock:
                    message_id = 900 + len(sent) + 1
                    sent.append(str(message_id))
                send_started.set()
                time.sleep(0.15)
                return {"message_id": message_id}

            def fake_edit(
                _chat_id: str,
                message_id: str,
                _text: str,
                _reply_markup: dict[str, object] | None = None,
                _parse_mode_name: str | None = None,
            ) -> bool:
                with event_lock:
                    edited.append(message_id)
                return True

            def update() -> None:
                try:
                    results.append(
                        ingest_bot.update_task_card_message(task_id, force=True)
                    )
                except BaseException as exc:  # pragma: no cover - assertion aid
                    errors.append(exc)

            with (
                mock.patch.object(ingest_bot, "task_card_store", store),
                mock.patch.object(
                    ingest_bot,
                    "task_card_update_lock",
                    threading.Lock(),
                ),
                mock.patch.object(ingest_bot, "task_card_last_update", {}),
                mock.patch.object(ingest_bot, "send_message", fake_send),
                mock.patch.object(ingest_bot, "edit_message_text", fake_edit),
            ):
                first = threading.Thread(target=update)
                first.start()
                self.assertTrue(send_started.wait(timeout=2))
                second = threading.Thread(target=update)
                second.start()
                first.join(timeout=3)
                second.join(timeout=3)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, [True, True])
            self.assertEqual(sent, ["901"])
            self.assertEqual(edited, ["901"])
            self.assertEqual(store.get(task_id)["status_message_id"], "901")


if __name__ == "__main__":
    unittest.main()
