"""Independent regression tests; all state and audio fixtures stay in local tempdirs.

The bot is intentionally not imported: importing it creates persistent application
state.  Small entry points are compiled from its real AST and exercised with fake
queues/Telegram calls plus the actual task-card SQLite implementation.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bot"))

from lx_fallback import (  # noqa: E402
    AppleTrack,
    candidate_from_item,
    classify_apple_failure,
    fallback_is_eligible,
    rank_download_candidates,
    LxFallbackError, LxFallbackStopped, LxCandidate, is_lossless_quality,
)
from task_cards import TaskCardStore, delete_recorded_paths, json_paths  # noqa: E402
from unified_music import (  # noqa: E402
    RecordingCandidate, UnifiedRecording, bounded_search, merge_candidates,
    strict_match, unified_eligible,
)
from sq_music import (  # noqa: E402
    SqMusicClient, SqMusicError, SqMusicStopped, item_from_record,
    select_candidate as select_sq_candidate,
)
from apple_music_results import effective_download_return_code  # noqa: E402


def ingest_namespace(*names: str, **overrides: Any) -> dict[str, Any]:
    """Load named real functions, avoiding module-level state or network access."""
    tree = ast.parse((ROOT / "bot" / "ingest_bot.py").read_text())
    wanted = set(names)
    nodes = [node for node in tree.body if getattr(node, "name", None) in wanted]
    found = {node.name for node in nodes}
    if found != wanted:
        raise AssertionError(f"Missing bot entry points: {sorted(wanted - found)}")
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes]
    namespace = {
        "__name__": __name__, "dataclass": dataclass, "Path": Path,
        "Any": Any, "json": json, "time": time, "hashlib": hashlib,
        "threading": threading,
    }
    namespace.update(overrides)
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(ROOT / "bot" / "ingest_bot.py"), "exec"), namespace)
    return namespace


class ExistingFailureBoundaries(unittest.TestCase):
    def test_only_deterministic_apple_missing_media_allows_fallback(self):
        self.assertEqual(classify_apple_failure(66, "No downloadable media found"), "no_downloadable_media")
        for code, text in (
            (1, "HTTP Error 401: Unauthorized"),
            (1, "HTTP Error 403: Forbidden"),
            (1, "Read timed out"),
            (1, "Network is unreachable"),
            (1, "Error fetching account info"),
            (70, "Wrapper m3u8 request failed after 3 attempts"),
            (67, 'API Error {"failureType":"5000"} 暂时无法收听此歌曲'),
            (0, "No downloadable media found"),
        ):
            with self.subTest(code=code, failure=text):
                self.assertIsNone(classify_apple_failure(code, text))

    def test_atmos_multitrack_and_missing_identity_never_use_legacy_fallback(self):
        defaults = dict(enabled=True, mode="alac", pending_url_count=1,
                        track_id="123", return_code=66,
                        failure_text="No downloadable media found")
        self.assertIsNotNone(fallback_is_eligible(**defaults))
        for changes in ({"mode": "atmos"}, {"mode": "both"},
                        {"pending_url_count": 2}, {"pending_url_count": 0},
                        {"track_id": ""}, {"enabled": False}):
            with self.subTest(changes=changes):
                self.assertIsNone(fallback_is_eligible(**{**defaults, **changes}))

    def test_lx_lossless_candidates_exclude_320k(self):
        track = AppleTrack("123", "晴天", "周杰伦", "叶惠美", 269)
        item = {"source": "kw", "songmid": "k1", "name": track.title,
                "singer": track.artist, "albumName": track.album,
                "interval": "4:29", "types": {"320k": {"size": 1234}}}
        self.assertEqual(rank_download_candidates(track, [(0, item)], allow_320=False), [])
        item["types"]["flac"] = {"size": 5000}
        candidates = rank_download_candidates(track, [(0, item)], allow_320=False)
        self.assertTrue(candidates)
        self.assertTrue(all(candidate.quality != "320k" for candidate in candidates))

    def test_lx_wrong_version_or_duration_is_rejected(self):
        track = AppleTrack("123", "晴天", "周杰伦", "叶惠美", 269)
        base = {"source": "kw", "songmid": "k1", "name": track.title,
                "singer": track.artist, "albumName": track.album,
                "interval": "4:29", "types": {"flac": {"size": 5000}}}
        self.assertIsNotNone(candidate_from_item(track, base, quality="flac", source_rank=0))
        for changed in ({"name": "晴天 (Live)"}, {"name": "晴天 (伴奏)"},
                        {"interval": "3:20"}, {"singer": "其他歌手"}):
            self.assertIsNone(candidate_from_item(track, {**base, **changed}, quality="flac", source_rank=0))


class UnifiedSearchAndRecordingIdentity(unittest.TestCase):
    def candidate(self, provider="apple", **changes):
        values = dict(provider=provider, source=provider, source_id="123",
                      title="晴天", artist="周杰伦", album="叶惠美",
                      duration_seconds=269.0, isrc="TWJVR0301001",
                      quality="alac" if provider == "apple" else "flac")
        values.update(changes)
        return RecordingCandidate(**values)

    def test_merge_prefers_apple_then_lx_then_sq_and_deduplicates_source_id(self):
        apple, lx, sq = (self.candidate(provider) for provider in ("apple", "lx", "sq"))
        groups = merge_candidates([sq, lx, sq, apple])
        self.assertEqual(len(groups), 1)
        self.assertEqual([item.provider for item in groups[0].candidates], ["apple", "lx", "sq"])

    def test_version_markers_in_title_or_album_do_not_automatically_merge(self):
        original = self.candidate()
        for changes in ({"title": "晴天 (Live)"}, {"title": "晴天 (伴奏)"},
                        {"album": "叶惠美 (2018 Remastered)"},
                        {"album": "叶惠美 (Acoustic)"},
                        {"album": "现场演唱会"}, {"duration_seconds": 273},
                        {"isrc": "TWJVR0309999"}, {"artist": "其他歌手"}):
            with self.subTest(changes=changes):
                wrong = self.candidate("sq", **changes)
                self.assertFalse(strict_match(original, wrong))
                self.assertEqual(len(merge_candidates([original, wrong])), 2)

    def test_duration_without_album_is_sufficient_but_unknown_version_is_separate(self):
        original = self.candidate(isrc="", album="")
        matched = self.candidate("lx", isrc="", album="", duration_seconds=270.5)
        self.assertTrue(strict_match(original, matched))
        unknown = self.candidate("sq", isrc="", album="", duration_seconds=None)
        self.assertFalse(strict_match(original, unknown))

    def test_different_live_performances_do_not_merge_even_at_same_duration(self):
        first = self.candidate(title="晴天 (Live)", album="2010 Live Concert")
        second = self.candidate("sq", title="晴天 (Live)", album="2011 Live Concert")
        self.assertFalse(strict_match(first, second))
        self.assertEqual(len(merge_candidates([first, second])), 2)

    def test_different_remaster_editions_do_not_merge_even_at_same_duration(self):
        first = self.candidate(title="晴天 (Remastered)", album="叶惠美 2011 Remaster")
        second = self.candidate("lx", title="晴天 (Remastered)", album="叶惠美 2018 Remaster")
        self.assertFalse(strict_match(first, second))
        self.assertEqual(len(merge_candidates([first, second])), 2)

    def test_pairwise_matching_cannot_bridge_two_incompatible_durations(self):
        # 269 and 272 each match 270.5, but cannot share a download selection.
        left = self.candidate(duration_seconds=269)
        bridge = self.candidate("lx", duration_seconds=270.5)
        right = self.candidate("sq", duration_seconds=272)
        groups = merge_candidates([bridge, right, left])
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(all(strict_match(a, b) for a in group.candidates
                                for b in group.candidates) for group in groups))

    def test_payload_roundtrip_preserves_platform_metadata_and_rejects_conflicts(self):
        apple = self.candidate(url="https://music.apple.com/cn/album/example/12?i=123")
        sq = self.candidate("sq", metadata={"musicId": "123", "source": "netease",
                                              "downloadTypes": [{"type": "flac"}]})
        recording = UnifiedRecording((apple, sq))
        payload = json.loads(json.dumps(recording.to_payload(), ensure_ascii=False))
        self.assertEqual(UnifiedRecording.from_payload(payload), recording)
        conflict = copy.deepcopy(payload)
        conflict["candidates"][1]["title"] = "晴天 (Live)"
        with self.assertRaises(ValueError):
            UnifiedRecording.from_payload(conflict)
        payload["schema"] = 999
        with self.assertRaises(ValueError):
            UnifiedRecording.from_payload(payload)

    def test_hung_source_returns_on_deadline_and_cannot_mutate_results_later(self):
        release = threading.Event()
        returned: dict[str, list[Any]] = {}
        errors: dict[str, str] = {}
        try:
            started = time.monotonic()
            returned, errors = bounded_search({
                "apple": lambda: ["apple-result"],
                "lx": lambda: ["lx-result"],
                "sq": lambda: (release.wait(2), ["late-sq"])[1],
            }, timeout=0.04)
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertEqual(returned.get("apple"), ["apple-result"])
            self.assertEqual(returned.get("lx"), ["lx-result"])
            self.assertEqual(errors.get("sq"), "timeout")
            snapshot = copy.deepcopy(returned)
        finally:
            release.set()
        time.sleep(0.03)
        self.assertEqual(returned, snapshot)

    def test_search_source_exception_is_partial_and_contains_no_exception_secrets(self):
        def failed():
            raise RuntimeError("password=must-not-appear")
        results, errors = bounded_search({"apple": failed, "sq": lambda: ["one"]}, timeout=0.2)
        self.assertEqual(results, {"sq": ["one"]})
        self.assertEqual(errors, {"apple": "RuntimeError"})

    def test_unification_only_accepts_single_lossless_song_request(self):
        song = "https://music.apple.com/cn/album/example/12?i=123"
        self.assertTrue(unified_eligible(True, "alac", [song]))
        self.assertTrue(unified_eligible(True, "alac", ["https://music.apple.com/cn/song/example/123"]))
        self.assertTrue(unified_eligible(True, "alac", ["unified://recording/abc"]))
        for enabled, mode, urls in (
            (True, "atmos", [song]), (True, "both", [song]),
            (True, "alac", [song, song]), (True, "alac", []),
            (False, "alac", [song]),
            (True, "alac", ["https://music.apple.com/cn/album/example/12"]),
            (True, "alac", ["https://music.apple.com.attacker.example/cn/song/123"]),
            (True, "alac", ["https://youtube.com/watch?v=123"]),
        ):
            with self.subTest(enabled=enabled, mode=mode, urls=urls):
                self.assertFalse(unified_eligible(enabled, mode, urls))


class PersistentQueueAndDeletion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sqbot-guards-")
        self.root = Path(self.tmp.name)
        self.store = TaskCardStore(self.root / "task-cards.sqlite3")
        self.jobs = queue.Queue()
        self.ns = ingest_namespace(
            "Job", "job_record", "queue_download", "recover_task_card_jobs",
            task_card_store=self.store, job_queue=self.jobs,
            append_journal=Mock(), update_task_card_message=Mock(),
            queue_status=lambda: "queue", log=Mock(),
            unified_eligible=unified_eligible, UNIFIED_MUSIC_ENABLED=True,
        )

    def tearDown(self):
        self.store._connection.close()
        self.tmp.cleanup()

    def test_selection_payload_survives_restart_and_repeat_submit_is_idempotent(self):
        payload = {"unified_music": {"schema": 1, "title": "晴天",
                   "candidates": [{"provider": "sq", "source_id": "987",
                                   "metadata": {"id": "987", "source": "netease"}}]}}
        self.ns["queue_download"]("telegram", "chat1", "msg1", [], "alac", payload=payload)
        first = self.jobs.get_nowait()
        self.ns["queue_download"]("telegram", "chat1", "msg1", [], "alac", payload=payload)
        self.assertTrue(self.jobs.empty())
        self.assertEqual(len(self.store.list_statuses(["queued"])), 1)
        self.store.update(first.task_id, status="running", phase="SQ 下载中")
        # Open a fresh connection, as a service restart would.
        self.store._connection.close()
        self.store = TaskCardStore(self.root / "task-cards.sqlite3")
        self.ns["task_card_store"] = self.store
        self.assertEqual(self.ns["recover_task_card_jobs"](), 1)
        recovered = self.jobs.get_nowait()
        self.assertEqual(recovered.task_id, first.task_id)
        self.assertEqual(recovered.payload, payload)
        self.assertEqual(recovered.kind, "apple_music")
        self.assertEqual(recovered.mode, "alac")
        self.assertEqual(self.ns["job_record"](recovered)["payload"], payload)

    def test_completed_and_user_cancelled_tasks_are_not_restarted(self):
        for index, status in enumerate(("completed", "failed", "stopped", "deleted")):
            self.ns["queue_download"]("telegram", "c", str(index), [], "alac")
            job = self.jobs.get_nowait()
            self.store.update(job.task_id, status=status)
        self.assertEqual(self.ns["recover_task_card_jobs"](), 0)
        self.assertTrue(self.jobs.empty())

    def test_direct_single_apple_song_enters_unified_queue_with_persistent_identity(self):
        song = "https://music.apple.com/cn/album/example/12?i=123"
        self.ns["queue_download"]("telegram", "c", "single", [song], "alac")
        job = self.jobs.get_nowait()
        self.assertIn("unified_music", job.payload)
        self.assertEqual(job.payload["unified_music"]["apple_url"], song)
        stored = self.store.job_from_task(self.store.get(job.task_id))
        self.assertEqual(stored["payload"], job.payload)

    def test_atmos_albums_multilink_and_video_queue_payload_remain_legacy(self):
        song = "https://music.apple.com/cn/album/example/12?i=123"
        cases = [("apple_music", "atmos", [song]), ("apple_music", "both", [song]),
                 ("apple_music", "alac", [song, song]),
                 ("apple_music", "alac", ["https://music.apple.com/cn/album/example/12"]),
                 ("youtube_audio", "audio", ["https://youtube.com/watch?v=123"]),
                 ("youtube_video", "video", ["https://youtube.com/watch?v=123"]),
                 ("bilibili_audio", "audio", ["https://bilibili.com/video/BV123"]),
                 ("bilibili_video", "video", ["https://bilibili.com/video/BV123"])]
        for index, (kind, mode, urls) in enumerate(cases):
            with self.subTest(kind=kind, mode=mode):
                self.ns["queue_download"]("telegram", "c", f"legacy-{index}", urls, mode, kind)
                job = self.jobs.get_nowait()
                self.assertIsNone(job.payload)
                self.assertEqual(job.kind, kind)
                self.assertEqual(job.urls, urls)

    def test_delete_never_removes_root_or_symlink_target_outside_root(self):
        library = self.root / "library"
        library.mkdir()
        shared = self.root / "other-service.flac"
        shared.write_bytes(b"fLaCshared")
        link = library / "outside.flac"
        link.symlink_to(shared)
        for candidate in (library, shared, link):
            deleted, rejected = delete_recorded_paths([candidate], [library])
            self.assertEqual(deleted, [])
            self.assertTrue(rejected)
            self.assertTrue(shared.exists())
            self.assertTrue(library.exists())

    def test_invalid_deletion_request_is_atomic_before_any_file_removal(self):
        library = self.root / "library"
        library.mkdir()
        own = library / "own.flac"
        own.write_bytes(b"fLaCown")
        shared = self.root / "other-service.flac"
        shared.write_bytes(b"fLaCshared")
        deleted, rejected = delete_recorded_paths([own, shared], [library])
        self.assertEqual(deleted, [])
        self.assertTrue(rejected)
        self.assertTrue(own.exists())
        self.assertTrue(shared.exists())


class UnifiedDownloadScheduling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sqbot-order-")
        self.root = Path(self.tmp.name).resolve()
        self.apple_path, self.lx_path, self.sq_path = [self.root / name for name in ("apple.m4a", "lx.flac", "sq.flac")]
        for path in (self.apple_path, self.lx_path, self.sq_path):
            path.write_bytes(b"audio-fixture")
        self.events = []
        self.code = 66
        self.failure = "No downloadable media found"
        self.lx_success = False
        self.changed = []
        self.identities = {str(self.apple_path): ("123", "alac", "晴天", "", "")}
        self.apple = RecordingCandidate("apple", "apple", "123", "晴天", "周杰伦", "叶惠美", 269,
                                        url="https://music.apple.com/cn/album/example/12?i=123", quality="alac")
        self.sq = SimpleNamespace(source="kw", song_id="s1", title="晴天", artist="周杰伦",
                                  album="叶惠美", duration_seconds=269, quality="kw_flac_1411",
                                  artwork_url="", song_info={})
        self.store = Mock()
        self.store.get.return_value = {"delete_paths_json": "[]"}
        self.ns = ingest_namespace(
            "Job", "LxFallbackOutcome", "job_record", "process_unified_music_job",
            "unique_path_strings", "deletion_targets_for_media", "sq_recording_candidate",
            "unified_owned_deletion_paths", "unified_sq_candidates",
            "unified_apple_paths_for_song", "unified_download_phase",
            json_paths=json_paths,
            replace=replace, RecordingCandidate=RecordingCandidate, UnifiedRecording=UnifiedRecording,
            strict_match=strict_match, AppleTrack=AppleTrack,
            LxFallbackError=LxFallbackError, LxFallbackStopped=LxFallbackStopped,
            begin_download_progress=Mock(), clear_download_progress=Mock(),
            set_download_progress_phase=Mock(),
            update_task_card=Mock(), complete_task_card=Mock(), append_journal=Mock(),
            task_card_store=self.store, preserve_job_for_restart=lambda job: False,
            wait_for_task_ready=lambda job: True,
            apple_music_song_id_from_url=lambda url: "123",
            read_apple_media_identity=lambda path: self.identities.get(str(path), ("", "other", "", "", "")),
            deduplicate_apple_music_urls=lambda urls, mode: (urls, []),
            media_snapshot=lambda root: {}, changed_media_files=lambda before, after: self.changed,
            run_apple_with_retries=self.apple_download,
            effective_download_return_code=effective_download_return_code,
            verify_unified_apple_audio=lambda path: {"duration_seconds": 269},
            validate_recording_duration=lambda recording, info: None,
            read_apple_failure_text=lambda logfile, tail: self.failure,
            fallback_is_eligible=fallback_is_eligible,
            LX_FALLBACK_ALLOW_CATALOG_API_3076=True,
            ALAC_OUTPUT_PATH=self.root, run_lx_fallback=self.lx_download,
            SQ_MUSIC_SOURCES=("kw", "netease", "qqvip"),
            sq_music_client=lambda: SimpleNamespace(search_lossless=self.sq_match),
            publish_sq_lossless=self.sq_publish,
            sync_word_lyrics_for_paths=Mock(), trigger_navidrome_scan=lambda: True,
            redact_sensitive_text=str, log=Mock(),
        )
        self.job = self.ns["Job"]("telegram", "c", "m", [self.apple.url], "alac", "apple_music", 1, task_id=42,
                                    payload={"unified_music": UnifiedRecording((self.apple,)).to_payload()})

    def tearDown(self):
        self.tmp.cleanup()

    def apple_download(self, *args):
        self.events.append("apple")
        return self.code, self.root / "apple.log", [], []

    def lx_download(self, *args, **kwargs):
        self.events.append("lx")
        return self.ns["LxFallbackOutcome"](self.lx_success, "unified_apple_unavailable", "test",
                                            paths=(self.lx_path,) if self.lx_success else (),
                                            created_paths=(self.lx_path,) if self.lx_success else (),
                                            quality="flac" if self.lx_success else "")

    def sq_match(self, query, limit=30):
        self.events.append("sq-match")
        return [self.sq]

    def sq_publish(self, *args):
        self.events.append("sq-download")
        return self.sq_path, True, {"duration_seconds": 269}

    def test_apple_success_never_contacts_fallback_downloads(self):
        self.code, self.changed = 0, [self.apple_path]
        self.ns["process_unified_music_job"](self.job)
        self.assertEqual(self.events, ["apple"])
        self.ns["complete_task_card"].assert_called_once()

    def test_lx_success_stops_before_sq(self):
        self.lx_success = True
        self.ns["process_unified_music_job"](self.job)
        self.assertEqual(self.events, ["apple", "lx"])
        self.ns["complete_task_card"].assert_called_once()

    def test_apple_missing_then_lx_unavailable_continues_to_sq_lossless(self):
        self.ns["process_unified_music_job"](self.job)
        self.assertEqual(self.events, ["apple", "lx", "sq-match", "sq-download"])
        completion = self.ns["complete_task_card"].call_args.kwargs
        self.assertEqual(json.loads(completion["local_paths_json"]), [str(self.sq_path)])
        self.assertEqual(json.loads(completion["delete_paths_json"]), [str(self.sq_path)])

    def test_sq_failed_source_continues_next_source_at_highest_lossless_tier(self):
        netease = SimpleNamespace(**{**vars(self.sq), "source": "netease", "song_id": "n1", "quality": "netease_flac_3000"})
        higher_kw = SimpleNamespace(**{**vars(self.sq), "quality": "kw_flac_3000"})

        def search(query, limit=30):
            self.events.append("sq-match")
            return [netease, self.sq, higher_kw]

        attempted = []
        def publish(job, candidate, target):
            attempted.append((job.task_id, candidate.source, candidate.quality))
            if candidate.source == "kw":
                raise RuntimeError("private-token=must-never-be-logged")
            return self.sq_path, True, {"duration_seconds": 269}

        self.ns["sq_music_client"] = lambda: SimpleNamespace(search_lossless=search)
        self.ns["publish_sq_lossless"] = publish
        self.ns["process_unified_music_job"](self.job)
        self.assertEqual(attempted, [(42, "kw", "kw_flac_3000"), (42, "netease", "netease_flac_3000")])
        self.ns["complete_task_card"].assert_called_once()
        journal = self.ns["append_journal"].call_args.args[0]
        self.assertIn("SQ kw: RuntimeError", journal["source_attempts"])
        self.assertNotIn("private-token", str(journal))

    def test_sq_user_cancel_never_attempts_another_source(self):
        netease = SimpleNamespace(**{**vars(self.sq), "source": "netease", "song_id": "n1", "quality": "netease_flac_3000"})
        self.ns["sq_music_client"] = lambda: SimpleNamespace(search_lossless=lambda query, limit=30: [self.sq, netease])
        attempted = []
        def publish(job, candidate, target):
            attempted.append(candidate.source)
            raise LxFallbackStopped("cancelled by parent")
        self.ns["publish_sq_lossless"] = publish
        self.ns["process_unified_music_job"](self.job)
        self.assertEqual(attempted, ["kw"])
        self.ns["complete_task_card"].assert_not_called()
        self.assertEqual(self.ns["update_task_card"].call_args.kwargs["status"], "stopped")

    def test_apple_auth_or_network_error_never_contacts_lx_or_sq(self):
        for code, failure in ((1, "HTTP Error 401"), (1, "Read timed out"),
                              (72, "Wrapper m3u8 request failed after 3 attempts")):
            with self.subTest(code=code, failure=failure):
                self.events.clear()
                self.code, self.failure = code, failure
                self.ns["process_unified_music_job"](self.job)
                self.assertEqual(self.events, ["apple"])
                self.ns["complete_task_card"].assert_not_called()
                self.assertEqual(self.ns["update_task_card"].call_args.kwargs["status"], "failed")

    def test_partial_apple_file_prevents_automatic_source_switch(self):
        self.changed = [self.apple_path]
        self.ns["process_unified_music_job"](self.job)
        self.assertEqual(self.events, ["apple"])
        self.ns["complete_task_card"].assert_not_called()

    def test_unrelated_concurrent_metadata_change_is_not_claimed_by_parent(self):
        unrelated = self.root / "unrelated.m4a"
        unrelated.write_bytes(b"other-service-track")
        self.identities[str(unrelated)] = ("999", "alac", "无关歌曲", "", "")
        self.changed = [unrelated]
        self.ns["process_unified_music_job"](self.job)
        self.assertEqual(self.events, ["apple", "lx", "sq-match", "sq-download"])
        completed = self.ns["complete_task_card"].call_args.kwargs
        self.assertNotIn(str(unrelated), completed["local_paths_json"])
        self.assertNotIn(str(unrelated), completed["delete_paths_json"])
        self.assertEqual(unrelated.read_bytes(), b"other-service-track")

    def test_unknown_new_apple_output_blocks_fallback_for_manual_review(self):
        unknown = self.root / "unidentified.m4a"
        unknown.write_bytes(b"partial-output")
        self.changed = [unknown]
        self.ns["process_unified_music_job"](self.job)
        self.assertEqual(self.events, ["apple"])
        self.ns["complete_task_card"].assert_not_called()
        self.assertTrue(unknown.exists())

    def test_wrong_sq_version_does_not_start_download(self):
        self.sq.album = "叶惠美 (2018 Remastered)"
        self.ns["process_unified_music_job"](self.job)
        self.assertEqual(self.events, ["apple", "lx", "sq-match"])
        self.ns["complete_task_card"].assert_not_called()

    def test_real_lx_adapter_excludes_320k_and_wrong_album_version_before_cache_or_enqueue(self):
        raw = {"source": "kw", "songmid": "k1", "name": "晴天", "singer": "周杰伦",
               "albumName": "叶惠美", "interval": "4:29"}
        mp3 = LxCandidate(raw, "kw", "晴天", "周杰伦", "叶惠美", 269, 200, "320k")
        wrong = LxCandidate({**raw, "songmid": "k2", "albumName": "叶惠美 (Remastered)"},
                            "kw", "晴天", "周杰伦", "叶惠美 (Remastered)", 269, 200, "flac")
        client = Mock()
        client.find_download_candidates.return_value = [mp3, wrong]
        ns = ingest_namespace(
            "Job", "LxFallbackOutcome", "run_lx_fallback", "lx_recording_candidate",
            RecordingCandidate=RecordingCandidate, strict_match=strict_match,
            AppleTrack=AppleTrack, LxCandidate=LxCandidate,
            parse_duration_seconds=lambda value: 269,
            is_lossless_quality=is_lossless_quality,
            LxFallbackClient=Mock(return_value=client),
            LxFallbackError=LxFallbackError, LxFallbackStopped=LxFallbackStopped,
            read_apple_failure_text=lambda logfile, tail: "",
            apple_music_song_id_from_url=lambda url: "123",
            set_download_progress_phase=Mock(), update_task_card=Mock(),
            LX_FALLBACK_ENABLED=True, LX_FALLBACK_BASE_URL="http://lx.invalid",
            LX_FALLBACK_OUTPUT_PATH=self.root, LX_FALLBACK_AUTH_FILE="",
            LX_FALLBACK_SERVER_MUSIC_ROOT="/music", LX_FALLBACK_REQUEST_TIMEOUT_SECONDS=1,
            LX_FALLBACK_QUEUE_TIMEOUT_SECONDS=1, LX_FALLBACK_POLL_SECONDS=.1,
            LX_FALLBACK_SEARCH_SOURCES=("kw",), LX_FALLBACK_ALLOW_320=True,
            LX_FALLBACK_MAX_CANDIDATES=8, log=Mock(), redact_sensitive_text=str,
        )
        outcome = ns["run_lx_fallback"](self.job, self.job, "alac", 66, self.root / "log", [],
                                          recording=self.apple, unavailable_signature="unified_apple_unavailable")
        self.assertFalse(outcome.success)
        self.assertFalse(client.find_download_candidates.call_args.kwargs["allow_320"])
        client.cached_path.assert_not_called()
        client.enqueue_and_wait.assert_not_called()


class SearchSessionOwnership(unittest.TestCase):
    def setUp(self):
        self.selected = Mock()
        self.answer = Mock()
        self.result = SimpleNamespace(title="晴天", artist="周杰伦", album="叶惠美", url="unified://sq/123",
                                      track_id="123", unified_payload={"schema": 1, "candidates": []})
        self.session = SimpleNamespace(chat_id="chat-1", scope_key="chat-1:owner", query="晴天", mode="alac", results=[self.result])
        self.available = True
        def get_session(key):
            return self.session if self.available and key == "session" else None
        def invalidate(key):
            self.available = False
        self.ns = ingest_namespace(
            "handle_callback_query", "search_scope_key",
            VIDEO_DELETE_CALLBACK_PREFIX="vdelete", YOUTUBE_CALLBACK_PREFIX="youtube",
            BILIBILI_CALLBACK_PREFIX="bilibili", WORD_LYRICS_MAC_CALLBACK_PREFIX="lyrics",
            SEARCH_CALLBACK_PREFIX="music", log=Mock(), chat_allowed=lambda chat: True,
            answer_callback_query=self.answer, get_search_session=get_session,
            is_active_search_session=lambda key, session: True,
            invalidate_search_session=invalidate, queue_download=self.selected,
            append_journal=Mock(), close_search_message=Mock(),
            html_escape_text=lambda text: text, truncate_display_text=lambda text, count: text,
        )

    def callback(self, actor="owner", chat="chat-1"):
        return {"id": "callback-1", "data": "music:sel:session:1", "from": {"id": actor},
                "message": {"message_id": 900, "chat": {"id": chat}}}

    def test_other_actor_or_chat_cannot_consume_selection(self):
        for callback in (self.callback(actor="other"), self.callback(chat="another-chat")):
            with self.subTest(callback=callback):
                self.ns["handle_callback_query"](callback)
                self.selected.assert_not_called()
                self.assertTrue(self.available)
                self.assertTrue(self.answer.call_args.kwargs.get("show_alert"))

    def test_owner_can_select_once_and_unified_payload_is_preserved(self):
        self.ns["handle_callback_query"](self.callback())
        self.selected.assert_called_once()
        self.assertEqual(self.selected.call_args.kwargs["payload"], {"unified_music": self.result.unified_payload})
        self.ns["handle_callback_query"](self.callback())
        self.selected.assert_called_once()


class SqPublishingAndResumeOwnership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sqbot-publish-")
        self.root = Path(self.tmp.name).resolve()
        self.library, self.staging = self.root / "library", self.root / "staging"
        self.library.mkdir()
        self.staged = self.staging / "42" / "track.flac"
        self.staged.parent.mkdir(parents=True)
        self.staged.write_bytes(b"fLaC" + bytes(64))
        self.candidate = SimpleNamespace(source="kw", song_id="s1")
        self.recording = RecordingCandidate("sq", "kw", "s1", "晴天", "周杰伦", "叶惠美", 269)
        self.client = SimpleNamespace(download_candidate=Mock(return_value=self.staged))
        self.store = Mock()
        self.store.get.return_value = {"delete_paths_json": "[]"}
        self.verifier = Mock(return_value={"duration_seconds": 269})
        self.ns = ingest_namespace(
            "Job", "ensure_sq_publish_path", "publish_sq_lossless",
            "validate_recording_duration", "unified_owned_deletion_paths",
            "unique_path_strings", "deletion_targets_for_media",
            SQ_MUSIC_OUTPUT_PATH=self.library, SQ_MUSIC_STAGING_PATH=self.staging,
            sq_music_client=lambda: self.client, verify_lx_flac=self.verifier,
            wait_for_task_ready=lambda job: True, update_task_card=Mock(),
            LxFallbackError=LxFallbackError, LxFallbackStopped=LxFallbackStopped,
            task_card_store=self.store, json_paths=json_paths,
            re=re, shutil=shutil, os=os, secrets=secrets,
        )
        self.job = self.ns["Job"]("telegram", "c", "m", [], "alac", "apple_music", 1, task_id=42)
        identity = hashlib.sha256(b"kw:s1").hexdigest()[:16]
        self.destination = self.library / "周杰伦" / "叶惠美" / f"晴天 [{identity}].flac"

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self):
        return self.ns["publish_sq_lossless"](self.job, self.candidate, self.recording)

    def test_verified_copy_is_published_atomically_and_existing_file_is_reused(self):
        path, created, info = self.publish()
        self.assertEqual(path, self.destination)
        self.assertTrue(created)
        self.assertEqual(path.read_bytes(), self.staged.read_bytes())
        self.assertTrue(self.staged.exists())
        self.assertFalse(list(self.library.rglob("*.partial")))
        self.client.download_candidate.reset_mock()
        second, was_created, info = self.publish()
        self.assertEqual(second, path)
        self.assertFalse(was_created)
        self.client.download_candidate.assert_not_called()

    def test_racing_existing_destination_is_never_overwritten(self):
        previous = b"preexisting-verified-library-file"
        def racing_link(source, destination):
            Path(destination).write_bytes(previous)
            raise FileExistsError()
        self.ns["os"] = SimpleNamespace(fsync=os.fsync, link=racing_link)
        path, created, info = self.publish()
        self.assertFalse(created)
        self.assertEqual(path.read_bytes(), previous)
        self.assertFalse(list(self.library.rglob("*.partial")))

    def test_symlinked_sq_library_root_never_writes_or_adopts_external_files(self):
        external = self.root / "external-library"
        external.mkdir()
        sentinel = external / "keep.flac"
        sentinel.write_bytes(b"existing-other-service-file")
        self.library.rmdir()
        self.library.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "root is a symbolic link"):
            self.publish()
        self.client.download_candidate.assert_not_called()
        self.assertEqual(list(external.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"existing-other-service-file")
        self.assertTrue(self.staged.exists())

    def test_symlinked_artist_directory_never_writes_outside_sq_library(self):
        outside = self.root / "other-service"
        outside.mkdir()
        (self.library / "周杰伦").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            self.publish()
        self.client.download_candidate.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_destination_symlink_cannot_adopt_other_services_file(self):
        outside = self.root / "other-service.flac"
        outside.write_bytes(b"shared")
        self.destination.parent.mkdir(parents=True)
        self.destination.symlink_to(outside)
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertEqual(outside.read_bytes(), b"shared")

    def test_staging_escape_is_rejected_before_any_publication(self):
        outside = self.root / "outside.flac"
        outside.write_bytes(b"fLaC" + bytes(64))
        self.client.download_candidate.return_value = outside
        with self.assertRaises(RuntimeError):
            self.publish()
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.library.iterdir()), [])

    def test_cancel_after_copy_removes_only_partial_and_keeps_staging_private(self):
        checks = iter((True, False))
        self.ns["wait_for_task_ready"] = lambda job: next(checks)
        with self.assertRaises(LxFallbackStopped):
            self.publish()
        self.assertFalse(self.destination.exists())
        self.assertTrue(self.staged.exists())
        self.assertFalse(list(self.library.rglob("*.partial")))

    def test_resume_retains_only_this_tasks_previous_ownership_for_accepted_paths(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b"fLaC" + bytes(64))
        lyric = self.destination.with_suffix(".lrc")
        lyric.write_text("[00:00]test")
        unrelated = self.root / "another-task.flac"
        unrelated.write_bytes(b"shared")
        self.store.get.return_value = {"delete_paths_json": json.dumps([str(self.destination), str(lyric), str(unrelated)])}
        result = self.ns["unified_owned_deletion_paths"](self.job, [self.destination], [])
        self.assertEqual(result, [str(self.destination), str(lyric)])
        self.store.get.return_value = {"delete_paths_json": "[]"}
        self.assertEqual(self.ns["unified_owned_deletion_paths"](self.job, [self.destination], []), [])


class SqTaskCompletionAndIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sqbot-adapter-")
        self.root = Path(self.tmp.name).resolve()
        self.record = {"plugName": "kw", "id": "song-1", "name": "晴天",
                       "artistName": ["周杰伦"], "albumName": "叶惠美",
                       "duration": 269000, "brTypes": ["KW_Flac_1411"]}
        self.item = item_from_record(self.record)
        self.assertIsNotNone(self.item)
        self.now = 0.0
        self.client = SqMusicClient(
            "http://sqmusic.invalid:8099", "test-user", "test-password",
            staging_root=self.root, poll_interval_seconds=.1,
            monotonic=lambda: self.now, sleep=self.advance,
        )
        self.audio = self.root / "42" / "track.flac"
        self.audio.parent.mkdir()
        self.audio.write_bytes(b"fLaC" + bytes(64))
        self.task = {"id": 42, "downloadMusicId": "song-1",
                     "downloadPlugName": "kw", "downloadBrType": "KW_Flac_1411",
                     "downloadFile": "/music/42/track.flac", "downloadStatus": "success"}

    def tearDown(self):
        self.tmp.cleanup()

    def advance(self, amount):
        self.now += amount

    def test_only_explicit_source_flac_quality_is_eligible(self):
        self.assertEqual(self.item.duration_seconds, 269)
        for qualities in (["KW_Mp3_320"], ["flac"], ["QQVIP_Flac_1411"],
                          [{"type": "mp3", "id": "KW_Flac_1411"}], []):
            with self.subTest(qualities=qualities):
                self.assertIsNone(item_from_record({**self.record, "brTypes": qualities}))

    def test_same_title_without_version_evidence_is_not_enough(self):
        original = AppleTrack("123", "晴天", "周杰伦", "叶惠美", 269)
        matching = select_sq_candidate(original, [self.item])
        self.assertIsNotNone(matching)
        for changes in ({"name": "晴天 (Live)"}, {"duration": 280000},
                        {"artistName": ["其他歌手"]}):
            wrong = item_from_record({**self.record, **changes})
            self.assertIsNone(select_sq_candidate(original, [wrong]))
        unknown = item_from_record({**self.record, "albumName": "", "duration": None})
        self.assertIsNone(select_sq_candidate(original, [unknown]))

    def test_completed_task_must_match_requested_id_source_and_quality(self):
        self.assertEqual(self.client._task_file(self.task, self.item, 42), self.audio)
        for changes in ({"id": 43}, {"downloadMusicId": "another-song"},
                        {"downloadPlugName": "netease"},
                        {"downloadBrType": "KW_Mp3_320"}):
            with self.subTest(changes=changes), self.assertRaises(SqMusicError):
                self.client._task_file({**self.task, **changes}, self.item, 42)

    def test_task_file_cannot_escape_or_adopt_another_tasks_file(self):
        for path in ("/music/43/track.flac", "/music/42/../43/track.flac",
                     "/other/42/track.flac", "42/track.flac", "", "/music/42/track.mp3"):
            with self.subTest(path=path), self.assertRaises(SqMusicError):
                self.client._task_file({**self.task, "downloadFile": path}, self.item, 42)
        outside = self.root / "other-task.flac"
        outside.write_bytes(b"fLaC" + bytes(64))
        link = self.audio.parent / "symlink.flac"
        link.symlink_to(outside)
        with self.assertRaises(SqMusicError):
            self.client._task_file({**self.task, "downloadFile": "/music/42/symlink.flac"}, self.item, 42)
        self.audio.write_bytes(b"ID3" + bytes(64))
        with self.assertRaises(SqMusicError):
            self.client._task_file(self.task, self.item, 42)

    def test_enqueue_acceptance_and_loading_are_not_download_completion(self):
        calls = []
        states = iter(("waiting", "loading", "success"))
        def request(path, **kwargs):
            calls.append(path)
            if path == "/api/download/downloadSong":
                return {"id": 42}
            return {"task": {**self.task, "downloadStatus": next(states)}}
        self.client._request_json = request
        progress = []
        completed = self.client.download_candidate(self.item, on_progress=progress.append)
        self.assertEqual(completed, self.audio)
        self.assertEqual(progress, ["waiting", "loading", "success"])
        self.assertEqual(len([path for path in calls if path == "/api/task/bot/42"]), 3)

    def test_supplement_success_or_missing_actual_path_never_publishes_audio(self):
        for bad in ({"downloadStatus": "supplement_success"}, {"downloadFile": ""},
                    {"downloadMusicId": "another-song"}, {"downloadBrType": "KW_Mp3_320"}):
            with self.subTest(bad=bad):
                self.client._request_json = lambda path, **kwargs: (
                    {"id": 42} if path == "/api/download/downloadSong" else {"task": {**self.task, **bad}})
                with self.assertRaises(SqMusicError):
                    self.client.download_candidate(self.item)
                self.assertTrue(self.audio.exists())

    def test_user_cancel_before_enqueue_does_not_create_sq_task(self):
        request = Mock()
        self.client._request_json = request
        with self.assertRaises(SqMusicStopped):
            self.client.download_candidate(self.item, stop_requested=lambda: True)
        request.assert_not_called()

    def test_user_cancel_during_poll_does_not_publish_or_remove_shared_files(self):
        checks = iter((False, False, True))
        self.client._request_json = lambda path, **kwargs: (
            {"id": 42} if path == "/api/download/downloadSong" else
            {"task": {**self.task, "downloadStatus": "loading"}})
        with self.assertRaises(SqMusicStopped):
            self.client.download_candidate(self.item, stop_requested=lambda: next(checks))
        self.assertTrue(self.audio.exists())

    def test_timeout_never_treats_an_existing_file_as_success_or_deletes_it(self):
        self.client._request_json = lambda path, **kwargs: (
            {"id": 42} if path == "/api/download/downloadSong" else
            {"task": {**self.task, "downloadStatus": "loading"}})
        with self.assertRaises(SqMusicError):
            self.client.download_candidate(self.item, timeout_seconds=.15)
        self.assertTrue(self.audio.exists())


if __name__ == "__main__":
    unittest.main()
