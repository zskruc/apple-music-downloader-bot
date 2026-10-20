from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from word_lyrics_automation.queue import QueueStore

try:
    import word_lyrics
except ImportError:
    source = Path(__file__).resolve().parents[2] / "bot/word_lyrics.py"
    spec = importlib.util.spec_from_file_location("word_lyrics", source)
    word_lyrics = importlib.util.module_from_spec(spec)
    sys.modules["word_lyrics"] = word_lyrics
    spec.loader.exec_module(word_lyrics)


NATIVE = ('<tt xmlns="http://www.w3.org/ns/ttml" '
          'xmlns:i="http://music.apple.com/lyric-ttml-internal" i:timing="Word">'
          '<body><div><p begin="1" end="3"><span begin="1" end="2">Hello </span>'
          '<span begin="2" end="3">world</span></p><p begin="4" end="6">'
          '<span begin="4" end="5">Good </span><span begin="5" end="6">bye</span>'
          '</p></div></body></tt>')
LINE = '[00:01.00]Hello world\n[00:04.00]Good bye\n'


class NativeEntryGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio = self.root / "song.m4a"
        self.audio.write_bytes(b"immutable audio fixture")
        self.track = word_lyrics.Track(path=self.audio, title="Song", artist="Artist",
                                      song_id="123456789", duration=10)
        self.service = object.__new__(word_lyrics.WordLyricsService)
        self.service.state_dir = self.root / "state"
        self.service.audit = word_lyrics.AuditStore(self.service.state_dir)
        self.service.client = SimpleNamespace(fetch_word_ttml=lambda *_args, **_kwargs: ("cn", NATIVE))
        self.inspect = patch.object(word_lyrics, "inspect_track", return_value=self.track)
        self.inspect.start()
        self.addCleanup(self.inspect.stop)
        self.environment = patch.dict(os.environ, {
            "AMDL_WORD_LYRICS_ENABLED": "true", "AMDL_WORD_LYRICS_AUTOMATION_ENABLED": "true",
            "AMDL_WORD_LYRICS_STATE_DIR": str(self.service.state_dir),
            "AMDL_HAN_CHARACTER_TIMING_ENABLED": "true",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_unchanged_native_audio_is_accepted_and_reused(self):
        original = self.audio.read_bytes()
        first = self.service.process(self.track)
        second = self.service.process(self.track)
        self.assertEqual(first.status, "accepted")
        self.assertEqual(second.status, "cached")
        self.assertEqual(self.audio.read_bytes(), original)
        self.assertEqual(self.audio.with_suffix(".ttml").read_text(), NATIVE)

    def test_audio_change_during_fetch_never_publishes(self):
        def fetch(*_args, **_kwargs):
            self.audio.write_bytes(b"a different recording")
            return "cn", NATIVE
        self.service.client.fetch_word_ttml = fetch
        result = self.service.process(self.track)
        self.assertEqual(result.reason, "audio_changed_during_native_lyrics")
        self.assertFalse(self.audio.with_suffix(".ttml").exists())

    def test_post_write_audio_change_restores_the_prior_sidecar(self):
        target = self.audio.with_suffix(".ttml")
        prior = '<tt><body><p begin="1" end="3">Hello world</p></body></tt>'
        target.write_text(prior)
        real_write = self.service.write_sidecar
        def write(*args):
            result = real_write(*args)
            self.audio.write_bytes(b"concurrent recording replacement")
            return result
        with patch.object(self.service, "write_sidecar", side_effect=write):
            result = self.service.process(self.track)
        self.assertEqual(result.reason, "audio_changed_during_native_lyrics")
        self.assertEqual(target.read_text(), prior)
        self.assertTrue(list((self.root / "state/backups").rglob("*.ttml")))

    def test_post_write_audio_change_removes_a_new_sidecar(self):
        real_write = self.service.write_sidecar
        def write(*args):
            result = real_write(*args)
            self.audio.write_bytes(b"concurrent recording replacement")
            return result
        with patch.object(self.service, "write_sidecar", side_effect=write):
            result = self.service.process(self.track)
        self.assertEqual(result.reason, "audio_changed_during_native_lyrics")
        self.assertFalse(self.audio.with_suffix(".ttml").exists())

    def test_bad_native_and_cached_word_clocks_do_not_pass(self):
        variants = [
            NATIVE.replace('begin="1" end="2"', 'begin="8" end="2"'),
            NATIVE.replace('begin="1" end="2"', 'begin="0" end="2"'),
            NATIVE.replace('begin="2" end="3"', 'begin="2" end="2"'),
            NATIVE.replace('begin="1" end="2"', 'begin="nan" end="2"'),
            NATIVE.replace('begin="4" end="6"', 'begin="4" end="30"'),
        ]
        for invalid in variants:
            with self.subTest(invalid=invalid):
                target = self.audio.with_suffix(".ttml")
                target.write_text(invalid)
                self.assertIsNone(self.service.existing_word_sidecar(self.audio, duration=10))
                target.unlink()
                self.service.client.fetch_word_ttml = lambda *_args, **_kwargs: ("cn", invalid)
                result = self.service.process(self.track)
                self.assertEqual(result.status, "rejected")
                self.assertEqual(result.reason, "invalid_apple_word_timing")
                self.assertFalse(target.exists())

    def test_native_parallel_and_nested_background_cues_are_valid(self):
        background = ('<span xmlns:m="http://www.w3.org/ns/ttml#metadata" m:role="x-bg">'
                      '<span begin="1.4" end="2.5">Backing </span>'
                      '<span begin="2.5" end="3">voice</span></span>')
        parallel = NATIVE.replace('</p>', background + '</p>', 1)
        timed_parent = parallel.replace('m:role="x-bg"', 'm:role="x-bg" begin="1.4" end="3"')
        overlapping = NATIVE.replace('begin="2" end="3"', 'begin="1.5" end="3"')
        historical_offsets = NATIVE.replace('begin="1" end="2"', 'begin="1s" end="2s"')
        for valid in (parallel, timed_parent, overlapping, historical_offsets):
            with self.subTest(valid=valid):
                word_lyrics.validate_word_timing(valid, word_lyrics.parse_ttml(valid), duration=10)

    def test_native_container_rounding_is_bounded_and_does_not_relax_word_envelopes(self):
        within_rounding = NATIVE.replace('<body><div>', '<body dur="6s"><div end="6.011">')
        word_lyrics.validate_word_timing(within_rounding, word_lyrics.parse_ttml(within_rounding), duration=10)
        outside_rounding = within_rounding.replace('end="6.011"', 'end="6.021"')
        with self.assertRaisesRegex(ValueError, "exceeds_parent_envelope"):
            word_lyrics.validate_word_timing(outside_rounding, word_lyrics.parse_ttml(outside_rounding), duration=10)
        outside_word_envelope = NATIVE.replace('begin="5" end="6"', 'begin="5" end="6.011"')
        with self.assertRaisesRegex(ValueError, "exceeds_parent_envelope"):
            word_lyrics.validate_word_timing(outside_word_envelope,
                word_lyrics.parse_ttml(outside_word_envelope), duration=10)

    def test_existing_elrc_word_clocks_must_be_ordered_within_the_line(self):
        target = self.audio.with_suffix(".elrc")
        target.write_text('[00:01.00]<00:02.00>Hello <00:01.50>world\n')
        self.assertIsNone(self.service.existing_word_sidecar(self.audio))
        target.write_text('[00:01.00]<00:01.00>Hello <00:02.00>world\n')
        self.assertIsNotNone(self.service.existing_word_sidecar(self.audio))

    def test_wrapper_initialization_outage_still_queues_each_audio(self):
        other = self.root / "other.m4a"
        other.write_bytes(b"another immutable audio fixture")
        with patch.object(word_lyrics, "AppleMusicClient", side_effect=ConnectionError("offline")):
            summary = word_lyrics.sync_word_lyrics_for_paths([self.audio, other],
                notification_context={"source": "telegram", "chat_id": "fixture"})
        self.assertEqual(summary, {"error": 2, "queued": 2})
        queue = QueueStore(self.service.state_dir / "automation/queue.sqlite3")
        self.assertEqual(len(queue.active_jobs()), 2)
        self.assertTrue(all(job.metadata["initial_reason"] == "apple_wrapper_initialization_failed"
                            for job in queue.active_jobs()))

    def test_wrapper_outage_can_still_use_valid_cached_lyrics(self):
        self.audio.with_suffix(".ttml").write_text(NATIVE)
        with patch.object(word_lyrics, "AppleMusicClient", side_effect=ConnectionError("offline")):
            summary = word_lyrics.sync_word_lyrics_for_paths([self.audio])
        self.assertEqual(summary, {"cached": 1})

    def test_duplicate_active_enqueue_is_reported_and_completed_job_is_not_reset(self):
        self.assertTrue(word_lyrics.enqueue_completion_job(self.audio))
        self.assertTrue(word_lyrics.enqueue_completion_job(self.audio))
        queue = QueueStore(self.service.state_dir / "automation/queue.sqlite3")
        self.assertEqual(len(queue.active_jobs()), 1)
        claim = queue.claim("source", owner="fixture")
        queue.complete(claim.id, claim.lease_token, result={}, expected_statuses={"leased_source"})
        self.assertFalse(word_lyrics.enqueue_completion_job(self.audio))
        self.assertEqual(queue.get(claim.id).status, "completed")

    def test_native_rollback_preserves_a_concurrent_publisher(self):
        concurrent = NATIVE.replace("Hello", "Concurrent")
        real_write = self.service.write_sidecar
        def write(*args):
            result = real_write(*args)
            self.audio.with_suffix(".ttml").write_text(concurrent)
            self.audio.write_bytes(b"concurrent recording replacement")
            return result
        with patch.object(self.service, "write_sidecar", side_effect=write):
            result = self.service.process(self.track)
        self.assertEqual(result.reason, "audio_changed_during_native_lyrics")
        self.assertEqual(self.audio.with_suffix(".ttml").read_text(), concurrent)

    def test_character_rollback_preserves_a_concurrent_publisher(self):
        original = NATIVE.replace("Hello ", "山河").replace("world", "故人")
        target = self.audio.with_suffix(".ttml")
        target.write_text(original)
        count = 0
        def fingerprint(_path):
            nonlocal count
            count += 1
            if count == 2:
                target.write_text(NATIVE)
            return {"sha256": str(count)}
        with patch("word_lyrics_automation.fingerprint.media_fingerprint", side_effect=fingerprint):
            with self.assertRaisesRegex(RuntimeError, "audio changed"):
                self.service.normalize_character_sidecar(self.audio)
        self.assertEqual(target.read_text(), NATIVE)

    def test_rollback_rechecks_the_sidecar_after_copying_backup(self):
        import hashlib
        import shutil
        target = self.audio.with_suffix(".ttml")
        target.write_text(NATIVE)
        backup = self.root / "original.ttml"
        backup.write_text("prior line lyric")
        concurrent = NATIVE.replace("Hello", "Concurrent")
        copy = shutil.copy2
        def copy_then_replace(*args):
            copied = copy(*args)
            target.write_text(concurrent)
            return copied
        with patch.object(word_lyrics.shutil, "copy2", side_effect=copy_then_replace):
            restored = self.service.rollback_sidecar(target, str(backup),
                expected_sha256=hashlib.sha256(NATIVE.encode()).hexdigest())
        self.assertFalse(restored)
        self.assertEqual(target.read_text(), concurrent)
        self.assertFalse(list(self.root.glob(".*.rollback-*")))


if __name__ == "__main__":
    unittest.main()
