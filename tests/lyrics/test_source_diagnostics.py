from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from word_lyrics_automation.providers.kugou import KugouLyricCandidate
from word_lyrics_automation.providers.netease import NeteaseSong
from word_lyrics_automation.queue import QueueStore
from word_lyrics_automation.resolver import CandidateRejected, SourceResolutionEngine
from word_lyrics_automation.source_worker import SourceWorker


class Gateway:
    def __init__(self, *, reference: bool = False, reference_path: Path | None = None):
        self.track = SimpleNamespace(path=Path("/music/song.m4a"), title="Test Song",
            artist="Test Artist", album="Test Album", duration=60.0, song_id="", isrc="")
        self.reference = reference
        self.reference_path = reference_path

    def inspect(self, *_):
        return self.track

    def existing_word(self, *_):
        return None

    def references(self, *_):
        return [(self.reference_path, None)] if self.reference_path else []

    def alignment_payload(self, *_):
        return {"reference_text": "fixed trusted reference" if self.reference else "",
            "reference_lines": ([{"text": "甲乙", "begin": 1.0, "end": 2.0}]
                if self.reference else [])}

    def publish(self, *_args, **_kwargs):
        raise CandidateRejected("fixture_publication_rejected")


class SourceDiagnosticsTests(unittest.TestCase):
    def song(self, **changes):
        return replace(NeteaseSong("matched", "Test Song", "Test Artist", "Test Album", 60000), **changes)

    def kugou(self, **changes):
        return replace(KugouLyricCandidate("matched", "private-access-key", "Test Song",
            "Test Artist", 60000, "官方推荐歌词", 60, 7), **changes)

    def engine(self, gateway=None, *, songs=(), record=None, kugou=()):
        return SourceResolutionEngine(gateway or Gateway(),
            amll_client=SimpleNamespace(search=lambda **_: []),
            netease_search_function=lambda *_, **__: list(songs),
            netease_record_function=lambda _: record or {},
            kugou_search_function=lambda **_: list(kugou),
            kugou_fetch_function=lambda _: "private-lyric-body")

    def resolve(self, engine):
        return engine.resolve(media_path=engine.gateway.track.path, metadata={}, expected_fingerprint={})

    def events(self, result, provider, event):
        return [item for item in result.alignment_payload["source_observations"]
            if item.get("provider") == provider and item.get("event") == event]

    def test_search_identity_and_no_yrc_are_explained_without_changing_legacy_records(self):
        engine = self.engine(songs=[self.song(), self.song(song_id="wrong-title", title="Other Song"),
            self.song(song_id="wrong-artist", artist="Someone Else"),
            self.song(song_id="wrong-duration", duration_ms=62000)],
            record={"lrc": {"lyric": "private-line-lyric"}, "pureMusic": False})
        result = self.resolve(engine)
        observations = result.alignment_payload["source_observations"]
        self.assertEqual(observations[0], {"provider": "netease", "song": {
            "song_id": "matched", "title": "Test Song", "artist": "Test Artist",
            "album": "Test Album", "duration_ms": 60000}, "instrumental_flag": False,
            "line_text_available": True, "word_text_available": False})
        self.assertEqual(self.events(result, "netease", "search")[0]["result_count"], 4)
        reasons = {reason for item in self.events(result, "netease", "identity_rejected")
            for reason in item["reasons"]}
        self.assertEqual(reasons, {"title_mismatch", "artist_mismatch", "duration_mismatch"})
        self.assertEqual(self.events(result, "netease", "word_text_unavailable")[0]["reason"], "no_yrc")
        self.assertNotIn("private-line-lyric", json.dumps(observations))
        self.assertEqual(result.reason, "no_trusted_lyric_reference")

    def test_malformed_word_text_is_distinguished_from_absent_yrc(self):
        result = self.resolve(self.engine(songs=[self.song()], record={"yrc": {"lyric": "private-invalid-yrc"}}))
        self.assertEqual(self.events(result, "netease", "word_text_unavailable")[0]["reason"], "no_parseable_yrc")
        self.assertNotIn("private-invalid-yrc", json.dumps(result.alignment_payload["source_observations"]))

    def test_netease_exact_fusion_failures_retain_stage_without_lyric_or_exception_body(self):
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "song.ttml"
            reference.write_text("fixture", encoding="utf-8")
            report = SimpleNamespace(start_deltas=[0.0], end_deltas=[0.0], median_start_delta=0.0,
                maximum_start_delta=0.0, median_end_delta=0.0, skipped_source_indexes=[])
            for failure_stage in ("text", "clock", "units"):
                with self.subTest(stage=failure_stage):
                    engine = self.engine(Gateway(reference=True, reference_path=reference),
                        songs=[self.song()], record={"yrc": {"lyric": "[1000,1000](1000,1000,0)甲乙"}})
                    selected_report = (SimpleNamespace(**{**vars(report), "median_start_delta": 1.0})
                        if failure_stage == "clock" else report)
                    with patch("word_lyrics_automation.resolver.parse_line_references", return_value=[object()]), \
                            patch("word_lyrics_automation.resolver.match_exact_lines",
                                side_effect=RuntimeError("private-exception-body") if failure_stage == "text" else None,
                                return_value=selected_report), \
                            patch("word_lyrics_automation.resolver.retime_exact_lines_to_references",
                                side_effect=RuntimeError("private-exception-body")):
                        result = self.resolve(engine)
                    reasons = {"text": "reference_text_or_structure_mismatch",
                        "clock": "native_line_clock_mismatch", "units": "karaoke_unit_mapping_invalid"}
                    self.assertEqual(self.events(result, "netease", "fusion_rejected")[0]["reason"], reasons[failure_stage])
                    self.assertNotIn("private-exception-body", json.dumps(result.alignment_payload["source_observations"]))
                    self.assertTrue(self.events(result, "netease", "word_text_available"))

    def test_kugou_reference_identity_rejections_preserve_their_actual_gates(self):
        result = self.resolve(self.engine(kugou=[
            self.kugou(lyric_id="unofficial", product_from="user"),
            self.kugou(lyric_id="duration", duration_ms=61000),
            self.kugou(lyric_id="title", title="Other Song"),
            self.kugou(lyric_id="artist", artist="Someone Else")]))
        rejected = self.events(result, "kugou_official_reference", "identity_rejected")
        self.assertEqual({item["provider_id"]: item["reasons"] for item in rejected}, {
            "unofficial": ["not_official"], "duration": ["duration_mismatch"],
            "title": ["title_mismatch"], "artist": ["artist_mismatch"]})
        encoded = json.dumps(result.alignment_payload["source_observations"])
        self.assertNotIn("private-access-key", encoded)
        self.assertNotIn("private-lyric-body", encoded)

    def test_kugou_fusion_records_rejections_without_persisting_key_or_lyrics(self):
        for error, expected_reason in [
                ("provider word clock differs from the native recording", "line_clock_mismatch"),
                ("private-exception-body", "source_parse_or_fusion_rejected")]:
            with self.subTest(error=error):
                engine = self.engine(Gateway(reference=True), kugou=[self.kugou(),
                    self.kugou(lyric_id="unofficial", product_from="user"),
                    self.kugou(lyric_id="duration", duration_ms=64000)])
                with patch("word_lyrics_automation.resolver.parse_krc", return_value=[]), \
                        patch("word_lyrics_automation.resolver.fuse_reference_words", side_effect=ValueError(error)):
                    result = self.resolve(engine)
                self.assertEqual(self.events(result, "kugou_reference_fusion", "fusion_rejected")[0]["reason"], expected_reason)
                self.assertEqual(len(self.events(result, "kugou_reference_fusion", "identity_rejected")), 2)
                encoded = json.dumps(result.alignment_payload["source_observations"])
                for private in ("private-access-key", "private-lyric-body", "private-exception-body"):
                    self.assertNotIn(private, encoded)

    def test_observations_are_bounded_but_summary_counts_include_all_results(self):
        songs = [self.song(song_id=str(index), title="Other Song " + "x" * 1000) for index in range(500)]
        result = self.resolve(self.engine(songs=songs))
        observations = result.alignment_payload["source_observations"]
        self.assertLessEqual(len(observations), 128)
        summary = observations[-1]
        self.assertEqual(summary["counts"]["netease:identity_rejected"], 500)
        self.assertGreater(summary["omitted_events"], 0)
        self.assertTrue(all(len(item["song"]["title"]) <= 200
            for item in observations if item.get("event") == "identity_rejected"))

    def test_each_resolve_resets_diagnostics_before_the_next_track(self):
        engine = self.engine(songs=[self.song(song_id="wrong", title="Other Song")])
        self.resolve(engine)
        engine.netease_search = lambda *_, **__: []
        result = self.resolve(engine)
        self.assertFalse(self.events(result, "netease", "identity_rejected"))
        self.assertNotIn("netease:identity_rejected", result.alignment_payload["source_observations"][-1]["counts"])

    def test_legacy_record_metadata_is_bounded_without_copying_provider_secrets(self):
        engine = self.engine()
        song = self.song(song_id="id" * 1000, title="title" * 1000,
            artist="artist" * 1000, album="album" * 1000)
        engine.netease_records = {song.song_id: (song, {
            "lrc": {"lyric": "private-line-lyric"},
            "yrc": {"lyric": "private-word-lyric"}, "accesskey": "private-access-key"})}
        observations = engine.source_observations()
        self.assertEqual(observations[0]["song"], {
            "song_id": song.song_id[:200], "title": song.title[:200],
            "artist": song.artist[:200], "album": song.album[:200], "duration_ms": 60000})
        self.assertTrue(observations[0]["line_text_available"])
        self.assertTrue(observations[0]["word_text_available"])
        encoded = json.dumps(observations)
        self.assertLess(len(encoded), 1500)
        for private in ("private-line-lyric", "private-word-lyric", "private-access-key"):
            self.assertNotIn(private, encoded)

    def test_waiting_source_job_persists_safe_observations_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "song.m4a"
            media.write_bytes(b"test audio")
            store = QueueStore(root / "queue.sqlite3")
            stored = store.enqueue(media, max_attempts=1).job
            worker = object.__new__(SourceWorker)
            worker.store = store
            worker.engine = self.engine(songs=[self.song(song_id="wrong", title="Other Song")])
            worker.process_source(store.claim("source", owner="test"))
            after = QueueStore(root / "queue.sqlite3").get(stored.id)
            self.assertEqual(after.status, "waiting_source")
            self.assertTrue(after.payload["source_observations"])
            self.assertEqual(after.payload["source_diagnostics_history"][-1]["observations"],
                after.payload["source_observations"])


if __name__ == "__main__":
    unittest.main()
