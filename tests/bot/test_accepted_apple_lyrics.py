"""Exercise real producer functions with isolated media, queues and no network."""
from __future__ import annotations

import ast
import copy
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / "bot"
sys.path.insert(0, str(STAGE))
from apple_music_results import effective_download_return_code
from word_lyrics_automation.queue import QueueStore
import word_lyrics


def functions(*names, filename="ingest_bot.py", **overrides):
    source = STAGE / filename
    tree = ast.parse(source.read_text())
    nodes = [node for node in tree.body if getattr(node, "name", None) in names]
    if {node.name for node in nodes} != set(names):
        raise AssertionError("Missing real producer functions")
    namespace = {"__name__": __name__, "dataclass": dataclass, "Path": Path,
                 "Any": Any, "json": json, "time": time, "threading": threading,
                 "replace": replace}
    namespace.update(overrides)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])), str(source), "exec"), namespace)
    return namespace


class AcceptedAppleProducerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="accepted-apple-lyrics-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = [self.root / f"{i:02d} track.m4a" for i in range(1, 16)]
        for path in self.paths:
            path.write_bytes(b"isolated-media-fixture")
        self.before = {str(path): (1, 2) for path in self.paths[1:]}
        self.after = {str(path): (1, 2) for path in self.paths}
        self.existing = []
        self.stopped = False
        self.word_sync = Mock(return_value={"queued": 15})
        self.store = Mock()
        self.store.get.return_value = {"started_at": 1, "total_bytes": 30, "delete_paths_json": "[]"}
        self.ns = functions(
            "Job", "process_job", "process_unified_music_job", "sync_accepted_apple_word_lyrics",
            "unique_path_strings", "deletion_targets_for_media", "job_record", "unified_owned_deletion_paths",
            current_job_lock=threading.Lock(), current_job=None, stop_event=threading.Event(),
            wait_for_task_ready=lambda job: not self.stopped,
            task_should_stop=lambda job: self.stopped,
            preserve_job_for_restart=lambda job: False,
            task_card_store=self.store, update_task_card=Mock(), complete_task_card=Mock(),
            append_journal=Mock(), send_reply=Mock(), send_message=Mock(), log=Mock(),
            begin_download_progress=Mock(), clear_download_progress=Mock(),
            set_download_progress_phase=Mock(),
            deduplicate_apple_music_urls=lambda urls, mode: (urls if not self.existing else [], self.existing),
            output_for_mode=lambda mode: self.root,
            media_snapshot=Mock(side_effect=lambda root: self.before if not self.ns["run_download"].called else self.after),
            changed_media_files=lambda before, after: [p for p in after if before.get(p) != after[p]],
            run_download=Mock(return_value=(0, self.root / "download.log", [], self.paths[1:])),
            effective_download_return_code=effective_download_return_code,
            is_wrapper_m3u8_failure=lambda *args: False, is_transient_gamdl_failure=lambda *args: False,
            WRAPPER_M3U8_FAILURE_JOB_RETRIES=0, TRANSIENT_FAILURE_JOB_RETRIES=0,
            run_lx_fallback=Mock(return_value=None), redact_sensitive_text=str,
            sync_word_lyrics_for_paths=self.word_sync, trigger_navidrome_scan=Mock(return_value=True),
            json_paths=lambda value: json.loads(value or "[]"),
        )
        self.job = self.ns["Job"]("maintenance", "codex-local", "album-retry", ["https://music.apple.com/us/album/test/1440863506"], "alac", "apple_music", 1, task_id=118)

    def run_job(self, job=None):
        self.ns["process_job"](job or self.job)

    def test_one_new_and_fourteen_verified_existing_are_reconciled_once(self):
        self.run_job()
        self.word_sync.assert_called_once()
        self.assertEqual(set(self.word_sync.call_args.args[0]), {str(path) for path in self.paths})
        self.assertIsNone(self.word_sync.call_args.kwargs["notification_context"])
        self.assertEqual(self.word_sync.call_args.kwargs["source_url"], self.job.urls[0])
        completion = self.ns["complete_task_card"].call_args.kwargs
        self.assertEqual(len(json.loads(completion["local_paths_json"])), 15)
        self.assertEqual(json.loads(completion["delete_paths_json"]), [str(self.paths[0])])
        self.ns["send_message"].assert_not_called()

    def test_all_verified_existing_from_gamdl_still_register_lyrics_without_ownership(self):
        self.before = dict(self.after)
        self.ns["run_download"].return_value = (0, self.root / "download.log", [], self.paths)
        self.run_job()
        self.word_sync.assert_called_once()
        self.assertEqual(len(self.word_sync.call_args.args[0]), 15)
        self.assertEqual(json.loads(self.ns["complete_task_card"].call_args.kwargs["delete_paths_json"]), [])

    def test_all_prechecked_existing_skip_download_and_still_register_lyrics(self):
        self.existing = [SimpleNamespace(path=p, display_path=p.name) for p in self.paths]
        self.run_job()
        self.ns["run_download"].assert_not_called()
        self.word_sync.assert_called_once()
        self.assertEqual(len(self.word_sync.call_args.args[0]), 15)
        self.assertEqual(json.loads(self.ns["complete_task_card"].call_args.kwargs["delete_paths_json"]), [])

    def test_partial_failed_album_does_not_claim_completion_or_register_a_batch(self):
        self.ns["run_download"].return_value = (72, self.root / "download.log", ["[ERROR] wrapper playback failed"], self.paths[1:])
        self.run_job()
        self.word_sync.assert_not_called()
        self.ns["complete_task_card"].assert_not_called()
        self.assertEqual(self.ns["update_task_card"].call_args.kwargs["status"], "failed")
        self.assertTrue(self.ns["append_journal"].call_args.args[0]["failures"])

    def test_failed_partial_album_then_one_track_retry_registers_all_fifteen_once(self):
        self.before = {str(path): (1, 2) for path in self.paths[:4]}
        self.after = {str(path): (1, 2) for path in self.paths[:-1]}
        self.ns["run_download"].return_value = (72, self.root / "download.log", ["[ERROR] one playback failed"], self.paths[:4])
        self.run_job(replace(self.job, task_id=117))
        self.word_sync.assert_not_called()
        self.ns["complete_task_card"].assert_not_called()
        self.before = dict(self.after)
        self.after = {str(path): (1, 2) for path in self.paths}
        self.ns["run_download"].reset_mock()
        self.ns["run_download"].return_value = (0, self.root / "download.log", [], self.paths[:-1])
        self.run_job()
        self.word_sync.assert_called_once()
        self.assertEqual(set(self.word_sync.call_args.args[0]), {str(path) for path in self.paths})
        self.assertEqual(json.loads(self.ns["complete_task_card"].call_args.kwargs["delete_paths_json"]), [str(self.paths[-1])])

    def test_cancellation_during_download_never_registers_or_completes(self):
        def download(*args, **kwargs):
            self.stopped = True
            return 0, self.root / "download.log", [], self.paths[1:]
        self.ns["run_download"].side_effect = download
        self.run_job()
        self.word_sync.assert_not_called()
        self.ns["complete_task_card"].assert_not_called()
        self.assertEqual(self.ns["update_task_card"].call_args.kwargs["status"], "stopped")

    def test_cancellation_during_lyric_registration_is_rechecked_before_completion(self):
        def sync(*args, **kwargs):
            self.stopped = True
            return {"queued": 15}
        self.word_sync.side_effect = sync
        self.run_job()
        self.word_sync.assert_called_once()
        self.ns["complete_task_card"].assert_not_called()
        self.assertEqual(self.ns["update_task_card"].call_args.kwargs["status"], "stopped")

    def test_multiple_original_urls_are_not_misrepresented_as_the_first_storefront(self):
        job = replace(self.job, urls=[self.job.urls[0], "https://music.apple.com/jp/album/other/999999"])
        self.run_job(job)
        self.assertEqual(self.word_sync.call_args.kwargs["source_url"], "")
        self.assertEqual(self.ns["append_journal"].call_args.args[0]["job"]["urls"], job.urls)

    def test_only_new_audio_owns_sidecars_created_by_the_moved_hook(self):
        def sync(paths, **kwargs):
            for path in paths:
                Path(path).with_suffix(".ttml").write_text("native-lyric-fixture")
            return {"accepted": 15}
        self.word_sync.side_effect = sync
        self.run_job()
        owned = json.loads(self.ns["complete_task_card"].call_args.kwargs["delete_paths_json"])
        self.assertEqual(set(owned), {str(self.paths[0]), str(self.paths[0].with_suffix(".ttml"))})
        self.assertNotIn(str(self.paths[1].with_suffix(".ttml")), owned)

    def test_telegram_request_gets_one_consistent_batch_input(self):
        job = replace(self.job, source="telegram", chat_id="chat", message_id="original")
        self.run_job(job)
        self.word_sync.assert_called_once()
        self.assertEqual(self.word_sync.call_args.kwargs["notification_context"], {
            "source": "telegram", "chat_id": "chat", "message_id": "original", "task_id": 118,
        })

    def test_lyric_registration_error_is_recorded_without_falsifying_audio_failure(self):
        self.word_sync.side_effect = RuntimeError("registration unavailable")
        self.run_job()
        self.ns["complete_task_card"].assert_called_once()
        self.assertIn("RuntimeError", str(self.ns["log"].call_args_list))

    def configure_unified(self, *, existing=True, fallback=False):
        apple_url = "https://music.apple.com/us/album/test/1440863506?i=1440863663"
        path = self.paths[0]
        self.ns.update(
            UnifiedRecording=SimpleNamespace(from_payload=lambda payload: None),
            apple_music_song_id_from_url=lambda url: "1440863663",
            deduplicate_apple_music_urls=lambda urls, mode: ([], [SimpleNamespace(path=path)]) if existing else (urls, []),
            verify_unified_apple_audio=lambda path: {"duration_seconds": 200},
            run_apple_with_retries=Mock(return_value=(66 if fallback else 0, self.root / "download.log", [], [])),
            media_snapshot=Mock(side_effect=[{}, {str(path): (1, 2)}]),
            unified_apple_paths_for_song=lambda paths, url: [] if fallback else paths,
            unified_download_phase=Mock(), LX_FALLBACK_ENABLED=fallback, SQ_MUSIC_ENABLED=False,
            fallback_is_eligible=lambda **kwargs: "missing", read_apple_failure_text=lambda *args: "missing",
            LX_FALLBACK_ALLOW_CATALOG_API_3076=True,
            read_apple_media_identity=lambda path: ("1440863663", "alac", "title", "isrc", "fingerprint"),
            unified_recording_for_apple_url=lambda url: SimpleNamespace(artist="artist", title="title"),
            ALAC_OUTPUT_PATH=self.root,
        )
        if fallback:
            self.ns["media_snapshot"].side_effect = [{}, {}]
            self.ns["run_lx_fallback"].return_value = SimpleNamespace(success=True, paths=(path,), created_paths=(path,))
        return replace(self.job, payload={"unified_music": {"apple_url": apple_url}})

    def test_unified_existing_apple_gets_one_lyric_registration(self):
        job = self.configure_unified()
        self.ns["process_unified_music_job"](job)
        self.word_sync.assert_called_once()
        self.assertEqual(self.word_sync.call_args.args[0], [str(self.paths[0])])
        self.ns["run_apple_with_retries"].assert_not_called()
        self.ns["complete_task_card"].assert_called_once()
        self.assertEqual(json.loads(self.ns["complete_task_card"].call_args.kwargs["delete_paths_json"]), [])

    def test_unified_new_apple_is_registered_once_at_accepted_boundary(self):
        job = self.configure_unified(existing=False)
        self.ns["process_unified_music_job"](job)
        self.word_sync.assert_called_once()
        self.ns["complete_task_card"].assert_called_once()

    def test_unified_lx_keeps_existing_entry_and_does_not_use_new_apple_hook(self):
        job = self.configure_unified(existing=False, fallback=True)
        self.ns["sync_accepted_apple_word_lyrics"] = Mock(side_effect=AssertionError("LX must not re-enter Apple reconciliation"))
        self.ns["process_unified_music_job"](job)
        self.ns["sync_accepted_apple_word_lyrics"].assert_not_called()
        self.word_sync.assert_called_once()
        self.ns["complete_task_card"].assert_called_once()

    def test_unified_sq_keeps_existing_entry_and_does_not_use_new_apple_hook(self):
        job = self.configure_unified(existing=False, fallback=True)
        self.ns.update(
            LX_FALLBACK_ENABLED=False, SQ_MUSIC_ENABLED=True,
            run_lx_fallback=Mock(return_value=SimpleNamespace(success=False)),
            sq_music_client=Mock(return_value=object()),
            unified_sq_candidates=Mock(return_value=[SimpleNamespace(source="qq")]),
            publish_sq_lossless=Mock(return_value=(self.paths[0], True, {})),
        )
        self.ns["sync_accepted_apple_word_lyrics"] = Mock(side_effect=AssertionError("SQ must not re-enter Apple reconciliation"))
        self.ns["process_unified_music_job"](job)
        self.ns["sync_accepted_apple_word_lyrics"].assert_not_called()
        self.ns["publish_sq_lossless"].assert_called_once()
        self.word_sync.assert_called_once()
        self.ns["complete_task_card"].assert_called_once()

    def test_repeated_accepted_paths_only_enter_the_batch_once(self):
        self.ns["run_download"].return_value = (0, self.root / "download.log", [], self.paths + self.paths)
        self.run_job()
        self.word_sync.assert_called_once()
        self.assertEqual(len(self.word_sync.call_args.args[0]), 15)


class ExistingLyricAndQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="accepted-lyric-queue-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media = self.root / "known-track.m4a"
        self.media.write_bytes(b"immutable fixture audio")
        # sqlite3's transaction context does not close a connection. Keep
        # production connections alive until explicit fixture cleanup, so
        # Python 3.14's GC warning does not obscure these behavioral tests.
        self.connections = []
        original_connect = sqlite3.connect
        def tracking_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            self.connections.append(connection)
            return connection
        self.addCleanup(lambda: [connection.close() for connection in self.connections])
        patcher = patch.object(sqlite3, "connect", side_effect=tracking_connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_valid_word_clock_is_cached_without_source_fetch_or_audio_change(self):
        before = self.media.read_bytes()
        ttml = self.media.with_suffix(".ttml")
        ttml.write_text('<tt xmlns="http://www.w3.org/ns/ttml" xmlns:itunes="http://music.apple.com/lyric-ttml-internal" itunes:timing="Word"><body><div><p begin="0s" end="2s"><span begin="0s" end="1s">Hello</span> <span begin="1s" end="2s">world</span></p></div></body></tt>')
        service = word_lyrics.WordLyricsService.__new__(word_lyrics.WordLyricsService)
        service.client = Mock(side_effect=AssertionError("cached clock must not fetch sources"))
        original_ttml = ttml.read_bytes()
        with patch.object(word_lyrics, "inspect_track", return_value=word_lyrics.Track(self.media, duration=10)):
            result = service.process(word_lyrics.Track(self.media))
        self.assertEqual(result.status, "cached")
        self.assertEqual(result.reason, "valid_word_sidecar_already_exists")
        service.client.fetch_word_ttml.assert_not_called()
        self.assertEqual(self.media.read_bytes(), before)
        self.assertEqual(ttml.read_bytes(), original_ttml)

    def test_repeated_reconciliation_reuses_completed_instrumental_job_without_notifications(self):
        state = self.root / "state"
        store = QueueStore(state / "automation/queue.sqlite3")
        queued = store.enqueue(self.media, metadata={"title": "Instrumental"})
        lease = store.claim("source", owner="test")
        store.complete(queued.job.id, lease.lease_token, result={"instrumental": True}, expected_statuses={"leased_source"})
        track = word_lyrics.Track(self.media, title="Instrumental", duration=10)
        with patch.dict(os.environ, {"AMDL_WORD_LYRICS_AUTOMATION_ENABLED": "true", "AMDL_WORD_LYRICS_STATE_DIR": str(state)}), patch.object(word_lyrics, "inspect_track", return_value=track):
            self.assertFalse(word_lyrics.enqueue_completion_job(self.media))
            self.assertFalse(word_lyrics.enqueue_completion_job(self.media))
        current = store.get(queued.job.id)
        self.assertEqual(current.status, "completed")
        self.assertEqual(current.result, {"instrumental": True})
        with store.connect() as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM notification_subscriptions").fetchone()[0], 0)

    def test_same_telegram_batch_registration_does_not_duplicate_subscriptions(self):
        store = QueueStore(self.root / "queue.sqlite3")
        context = {"source": "telegram", "chat_id": "chat", "message_id": "msg", "task_id": 118, "batch_id": "all-accepted-paths"}
        one = store.enqueue(self.media, metadata={"notification": context})
        two = store.enqueue(self.media, metadata={"notification": context})
        self.assertEqual(one.job.id, two.job.id)
        with store.connect() as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM notification_subscriptions").fetchone()[0], 1)

    def real_sync_environment(self):
        service = SimpleNamespace(
            process=lambda track, **kwargs: word_lyrics.Result(str(track.path), "1440863663", "cached", "valid_word_sidecar_already_exists"),
            normalize_character_sidecar=lambda path: None,
            audit=SimpleNamespace(record=Mock()),
        )
        return service, {
            "AMDL_WORD_LYRICS_ENABLED": "true",
            "AMDL_WORD_LYRICS_AUTOMATION_ENABLED": "true",
            "AMDL_WORD_LYRICS_STATE_DIR": str(self.root / "real-state"),
        }

    def test_actual_full_batch_digest_and_subscriptions_are_stable_across_reentry(self):
        second = self.root / "second-track.m4a"
        second.write_bytes(b"second immutable audio")
        service, environment = self.real_sync_environment()
        context = {"source": "telegram", "chat_id": "chat", "message_id": "msg", "task_id": 118}
        with patch.dict(os.environ, environment), patch.object(word_lyrics, "service_from_environment", return_value=service), patch.object(word_lyrics, "inspect_track", side_effect=lambda path: word_lyrics.Track(Path(path), title=Path(path).stem, duration=10)):
            word_lyrics.sync_word_lyrics_for_paths([self.media, second], source_url="https://music.apple.com/us/album/test/1440863506", notification_context=context)
            word_lyrics.sync_word_lyrics_for_paths([second, self.media], source_url="https://music.apple.com/us/album/test/1440863506", notification_context=context)
        store = QueueStore(self.root / "real-state/automation/queue.sqlite3")
        with store.connect() as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM notification_subscriptions").fetchone()[0], 2)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM lyric_batch_messages").fetchone()[0], 1)

    def test_actual_maintenance_reconciliation_registers_jobs_without_notification_state(self):
        service, environment = self.real_sync_environment()
        ns = functions("Job", "sync_accepted_apple_word_lyrics", "unique_path_strings",
            sync_word_lyrics_for_paths=word_lyrics.sync_word_lyrics_for_paths,
            trigger_navidrome_scan=Mock(), log=Mock())
        job = ns["Job"]("maintenance", "codex-local", "repair", ["https://music.apple.com/us/album/test/1440863506"], "alac", "apple_music", 1, task_id=118)
        with patch.dict(os.environ, environment), patch.object(word_lyrics, "service_from_environment", return_value=service), patch.object(word_lyrics, "inspect_track", side_effect=lambda path: word_lyrics.Track(Path(path), title=Path(path).stem, duration=10)):
            ns["sync_accepted_apple_word_lyrics"](job, [self.media])
        store = QueueStore(self.root / "real-state/automation/queue.sqlite3")
        with store.connect() as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM notification_subscriptions").fetchone()[0], 0)
            self.assertIsNone(database.execute("SELECT name FROM sqlite_master WHERE name='lyric_batch_messages'").fetchone())


class DownloadBoundaryTests(unittest.TestCase):
    def test_successful_download_does_not_register_lyrics_before_parent_acceptance(self):
        with tempfile.TemporaryDirectory(prefix="gamdl-parent-boundary-") as directory:
            root = Path(directory)
            media = root / "downloaded.m4a"
            stream = io.StringIO("[INFO] one track downloaded\n")
            process = SimpleNamespace(stdout=stream, pid=1, poll=lambda: 0, wait=lambda **kwargs: 0)
            snapshot = Mock(side_effect=[{}, {str(media): (1, 2)}])
            word_sync = Mock(side_effect=AssertionError("per-download producer would duplicate the parent batch"))
            ns = functions(
                "Job", "run_gamdl", "log_path_for", output_for_mode=lambda mode: root,
                TEMP_PATH=root / "tmp", LOG_DIR=root / "logs",
                media_snapshot=snapshot, changed_media_files=lambda before, after: list(after),
                set_download_progress_context=Mock(), GAMDL_LYRIC_SIDECAR_FORMATS=("lrc",),
                sys=sys, os=os, subprocess=SimpleNamespace(Popen=lambda *args, **kwargs: process, PIPE=-1, STDOUT=-2),
                re=__import__("re"), deque=__import__("collections").deque,
                GAMDL_RUNNER=Path("gamdl_with_storefront.py"), WRAPPER_URL="http://wrapper",
                WRAPPER_DECRYPT_HOST="wrapper", WRAPPER_DECRYPT_PORT=10020,
                LANGUAGE="zh-CN", codec_priority_for_mode=lambda mode, job: "alac",
                GAMDL_USE_URL_STOREFRONT=True, storefront_from_url=lambda url: "us",
                set_active_task_process=Mock(), clear_active_task_process=Mock(),
                path_activity_signature=lambda path: (), process_tree_activity_signature=lambda pid: (),
                stop_event=threading.Event(), task_should_stop=lambda job: False, task_status=lambda job: "running",
                select=SimpleNamespace(select=lambda *args: ([stream], [], [])),
                update_download_progress_from_line=Mock(), GAMDL_FINISHED_WITH_ERRORS_RE=__import__("re").compile("Finished with [1-9]"),
                validated_verified_existing_path=lambda line: None,
                effective_download_return_code=effective_download_return_code,
                set_download_progress_phase=Mock(), run_gamdl_lyric_sidecars=Mock(return_value=0),
                sync_word_lyrics_for_paths=word_sync,
            )
            job = ns["Job"]("maintenance", "local", "test", ["https://music.apple.com/us/album/test/1440863506"], "alac", "apple_music", 1)
            code, _log, _tail, _existing = ns["run_gamdl"](job, "alac")
            self.assertEqual(code, 0)
            word_sync.assert_not_called()
            ns["run_gamdl_lyric_sidecars"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
