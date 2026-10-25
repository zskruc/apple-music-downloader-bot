from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from word_lyrics_automation.alignment_backend_whispercpp import (
    run_alignment, validate_dtw_result_clock,
)
from word_lyrics_automation.whisper_dtw import (
    NoTimedAcousticTokensError, convert_dtw_document,
)
from word_lyrics_automation.nas_alignment_worker import NasAlignmentWorker
from whispercpp_test_fixtures import dtw_document, model_bytes


def terminal_only(text="你"):
    document = dtw_document(text, language="zh", start_ms=1000)
    document["transcription"][0]["tokens"].pop()
    for token in document["transcription"][0]["tokens"]:
        token["t_dtw"] = 100
        token["offsets"] = {"from": 0, "to": 20000}
    return document


class NoTimedAcousticTokensTests(unittest.TestCase):
    def test_valid_unknown_groups_raise_typed_error_with_conservative_result(self):
        for text in ("你", "作詞測試者作曲測試者"):
            with self.subTest(text=text), self.assertRaises(NoTimedAcousticTokensError) as raised:
                convert_dtw_document(terminal_only(text))
            error = raised.exception
            self.assertIsInstance(error, RuntimeError)
            self.assertIn("no timed acoustic tokens with known group ends", str(error))
            self.assertEqual(error.result["segments"][0]["words"], [])
            self.assertEqual(error.result["segments"][0]["untimed_text"], text)
            diagnostics = error.result["clock_diagnostics"]
            self.assertEqual(diagnostics["timed_unit_count"], 0)
            self.assertEqual(diagnostics["untimed_character_count"], len(text))
            self.assertEqual(error.anchor_bounds, (1.0, 1.0))

    def test_malformed_dtw_never_becomes_a_recoverable_empty_observation(self):
        for value in (None, -1, float("nan"), "100"):
            document = terminal_only()
            document["transcription"][0]["tokens"][0]["t_dtw"] = value
            with self.subTest(value=value), self.assertRaises(RuntimeError) as raised:
                convert_dtw_document(document)
            self.assertNotIsInstance(raised.exception, NoTimedAcousticTokensError)


class WhisperCppZeroEvidenceRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.audio = self.root / "song.wav"
        with wave.open(str(self.audio), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(16000)
            target.writeframes(b"\0\0" * (25 * 16000))
        self.model = self.root / "model.bin"
        self.model.write_bytes(model_bytes())
        self.binary = self.root / "whisper-cli"
        self.binary.write_bytes(b"fixed test binary")
        self.payload = {
            "track": {"duration": 25, "title": "Song", "artist": "Artist"},
            "reference_text": "春风来了月亮出来",
            "reference_lines": [
                {"text": "春风来了", "begin": 3, "end": 7},
                {"text": "月亮出来", "begin": 13, "end": 17},
            ],
        }
        self.calls = []
        self.documents = {}

    def align(self):
        def prepare(arguments, **kwargs):
            shutil.copyfile(self.audio, arguments[-1])

        def infer(arguments, **kwargs):
            prefix = Path(arguments[arguments.index("-of") + 1])
            self.calls.append(prefix.name)
            prefix.with_suffix(".json").write_text(json.dumps(self.documents[prefix.name]))

        with patch("word_lyrics_automation.alignment_backend_whispercpp.subprocess.run", side_effect=prepare), \
             patch("word_lyrics_automation.alignment_backend_whispercpp._run_with_progress", side_effect=infer):
            return run_alignment(
                audio=self.audio, payload=self.payload,
                output=self.root / "candidate.ttml", report_path=self.root / "report.json",
                binary=self.binary, model=self.model, work_dir=self.root / "work",
                threads=1, timeout_seconds=300,
            )

    def good_crops(self):
        return {
            f"line-{index}": dtw_document(line["text"], language="zh", start_ms=1250, step_ms=500)
            for index, line in enumerate(self.payload["reference_lines"], 1)
        }

    def test_empty_whole_song_reaches_independent_reference_windows(self):
        self.documents = {"whisper-result": terminal_only(), **self.good_crops()}
        report = self.align()
        self.assertEqual(self.calls, ["whisper-result", "line-1", "line-2"])
        self.assertEqual(report["matched_reference_character_coverage"], 1)
        self.assertEqual(report["zero_evidence_acoustic_units"], ["whole-song"])
        self.assertEqual(report["completed_acoustic_units"], 2)
        self.assertEqual(report["clock_diagnostics"]["units"]["whole-song"]["timed_unit_count"], 0)
        self.assertFalse((self.root / "work/whole-song.checkpoint.json").exists())
        self.assertTrue((self.root / "work/line-2.checkpoint.json").exists())

    def test_empty_first_crop_continues_later_crops_and_uses_existing_whole_song_rescue(self):
        self.documents = {
            "whisper-result": dtw_document("春风来了", language="zh", start_ms=3000, step_ms=500),
            **self.good_crops(), "line-1": terminal_only(),
        }
        report = self.align()
        self.assertEqual(self.calls, ["whisper-result", "line-1", "line-2"])
        self.assertEqual(report["matched_reference_character_coverage"], 1)
        self.assertEqual(report["zero_evidence_acoustic_units"], ["line-1"])
        self.assertFalse((self.root / "work/line-1.checkpoint.json").exists())
        self.assertTrue((self.root / "work/line-2.checkpoint.json").exists())

    def test_all_unknown_observations_cannot_publish_from_reference_text(self):
        self.documents = {name: terminal_only() for name in ("whisper-result", "line-1", "line-2")}
        with self.assertRaisesRegex(RuntimeError, "reference or acoustic transcript is empty"):
            self.align()
        self.assertEqual(self.calls, ["whisper-result", "line-1", "line-2"])
        self.assertFalse((self.root / "candidate.ttml").exists())
        self.assertEqual(list((self.root / "work").glob("*.checkpoint.json")), [])

    def test_missing_crop_evidence_still_fails_existing_coverage_gate(self):
        self.documents = {
            "whisper-result": dtw_document("错误内容", language="zh", start_ms=3000, step_ms=500),
            **self.good_crops(), "line-1": terminal_only(),
        }
        with self.assertRaisesRegex(RuntimeError, "acoustic/reference mismatch"):
            self.align()
        self.assertEqual(self.calls, ["whisper-result", "line-1", "line-2"])
        self.assertFalse((self.root / "candidate.ttml").exists())

    def test_bad_clock_stops_before_retry_instead_of_becoming_empty(self):
        base = dtw_document("春风来了", language="zh", start_ms=3000, step_ms=500)
        variants = []
        for value in (None, -1):
            document = copy.deepcopy(base)
            document["transcription"][0]["tokens"][0]["t_dtw"] = value
            variants.append(document)
        document = copy.deepcopy(base)
        document["transcription"][0]["tokens"][1]["t_dtw"] = 100
        variants.append(document)
        variants.append(dtw_document("春风来了", language="zh", start_ms=24000, step_ms=500))
        for document in variants:
            with self.subTest(document=document):
                self.calls.clear()
                self.documents = {"whisper-result": document, **self.good_crops()}
                with self.assertRaises(RuntimeError):
                    self.align()
                self.assertEqual(self.calls, ["whisper-result"])
                self.assertFalse((self.root / "candidate.ttml").exists())

    def test_default_clock_validation_rejects_empty_result(self):
        with self.assertRaises(NoTimedAcousticTokensError) as raised:
            convert_dtw_document(terminal_only())
        with self.assertRaisesRegex(RuntimeError, "no timed acoustic words"):
            validate_dtw_result_clock(raised.exception.result, self.audio)

    def test_out_of_crop_terminal_anchor_is_fatal_before_whole_song_recovery(self):
        document = terminal_only()
        document["transcription"][0]["tokens"][0]["t_dtw"] = 2503
        self.documents = {"whisper-result": document, **self.good_crops()}
        with self.assertRaisesRegex(RuntimeError, "exceeds its actual audio crop"):
            self.align()
        self.assertEqual(self.calls, ["whisper-result"])
        self.assertFalse((self.root / "candidate.ttml").exists())

    def test_out_of_crop_terminal_anchor_is_fatal_inside_a_reference_crop(self):
        document = terminal_only()
        document["transcription"][0]["tokens"][0]["t_dtw"] = 603
        self.documents = {
            "whisper-result": dtw_document("错误内容", language="zh", start_ms=3000, step_ms=500),
            **self.good_crops(), "line-1": document,
        }
        # This reference crop is exactly 6 seconds, allowing one 20 ms frame.
        with self.assertRaisesRegex(RuntimeError, "exceeds its actual audio crop"):
            self.align()
        self.assertEqual(self.calls, ["whisper-result", "line-1"])
        self.assertFalse((self.root / "work/line-1.checkpoint.json").exists())

    def test_terminal_anchor_allows_exactly_one_boundary_frame_beyond_audio(self):
        document = terminal_only()
        document["transcription"][0]["tokens"][0]["t_dtw"] = 2502
        self.documents = {"whisper-result": document, **self.good_crops()}
        self.assertEqual(self.align()["matched_reference_character_coverage"], 1)

    def test_only_matching_typed_observation_can_enable_empty_clock_validation(self):
        with self.assertRaises(NoTimedAcousticTokensError) as raised:
            convert_dtw_document(terminal_only())
        observation = raised.exception
        with self.assertRaisesRegex(RuntimeError, "not a verified converter result"):
            validate_dtw_result_clock(copy.deepcopy(observation.result), self.audio,
                                      empty_observation=observation)
        observation.anchor_bounds = None
        with self.assertRaisesRegex(RuntimeError, "missing text-token anchor bounds"):
            validate_dtw_result_clock(observation.result, self.audio,
                                      empty_observation=observation)

    def test_no_text_tokens_can_recover_without_inventing_anchor_bounds(self):
        document = terminal_only()
        document["transcription"][0]["tokens"] = []
        self.documents = {"whisper-result": document, **self.good_crops()}
        report = self.align()
        self.assertEqual(report["matched_reference_character_coverage"], 1)
        self.assertEqual(report["zero_evidence_acoustic_units"], ["whole-song"])


class EmptyTranscriptWorkerExhaustionTests(unittest.TestCase):
    def test_both_models_exhausted_recheck_sources_and_finish_without_duplicate_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict("os.environ", {"AMDL_WORD_LYRICS_STATE_DIR": str(root / "state")}):
                worker = NasAlignmentWorker()
            audio = root / "song.wav"
            audio.write_bytes(b"unchanged recording")
            worker.model = root / "primary.bin"
            worker.model.write_bytes(b"primary model")
            worker.model_sha1 = hashlib.sha1(worker.model.read_bytes()).hexdigest()
            worker.fallback_model = root / "fallback.bin"
            worker.fallback_model.write_bytes(b"fallback model")
            worker.expected_fallback_model_sha1 = hashlib.sha1(worker.fallback_model.read_bytes()).hexdigest()
            worker.store.enqueue(audio)
            source = worker.store.claim("source", owner="test")
            worker.store.defer_to_alignment(source.id, source.lease_token, payload={
                "track": {"duration": 10}, "reference_text": "春风来了",
                "reference_lines": [{"text": "春风来了", "begin": 1, "end": 5}],
            }, reason="test")
            job = worker.store.claim("alignment", owner="test")
            with patch("word_lyrics_automation.nas_alignment_worker.run_alignment",
                       side_effect=RuntimeError("reference or acoustic transcript is empty")) as inference, \
                 patch.object(worker, "recover_from_sources", return_value=False) as recovery:
                worker.process(job)
            saved = worker.store.get(job.id)
            self.assertEqual(inference.call_count, 2)
            self.assertEqual(saved.status, "dead_letter")
            self.assertEqual(saved.payload["nas_quality_fallback"]["status"], "failed")
            self.assertNotIn("candidate_report", saved.payload)
            recovery.assert_called_once()


if __name__ == "__main__":
    unittest.main()
