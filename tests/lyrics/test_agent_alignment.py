"""Original synthetic acoustic regressions for independent vocal-agent clocks."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from word_lyrics_automation.alignment import (
    _reference_lines, build_candidate, reference_crop_bounds, validate_reference_payload,
)
from word_lyrics_automation.alignment_backend_whispercpp import run_alignment
from word_lyrics_automation.alignment_retry import build_with_reference_retry, reference_window_languages
from word_lyrics_automation.text import normalize_text
from word_lyrics_automation.ttml import parse_line_references, parse_word_ttml
from whispercpp_test_fixtures import dtw_document, model_bytes


def observation(text, begin, end):
    normalized = normalize_text(text)
    return {"segments": [{"words": [
        {"word": character, "start": begin + (end - begin) * index / len(normalized),
         "end": begin + (end - begin) * (index + 1) / len(normalized), "probability": .99}
        for index, character in enumerate(normalized)
    ]}]}


def payload_for(rows, duration=30):
    return {"track": {"title": "Test", "artist": "Test", "duration": duration},
            "reference_text": "".join(row["text"] for row in rows),
            "reference_lines": rows}


class AgentAlignmentTests(unittest.TestCase):
    def payload(self):
        return payload_for([
            {"text": "第一声部", "begin": 10, "end": 14, "key": "L1", "agent": "v1"},
            {"text": "第二句子", "begin": 15, "end": 20, "key": "L2", "agent": "v1"},
            {"text": "English backing", "begin": 14.9, "end": 19, "key": "L3", "agent": "v2"},
        ])

    def test_native_document_order_and_independent_voice_clocks_are_preserved(self):
        payload = self.payload()
        windows = [observation(row["text"], row["begin"], row["end"])
                   for row in payload["reference_lines"]]
        ttml, report = build_candidate({}, payload, reference_results=windows)
        lines = parse_word_ttml(ttml)
        self.assertEqual([(line.text, line.key, line.agent) for line in lines],
                         [(row["text"], row["key"], row["agent"])
                          for row in payload["reference_lines"]])
        self.assertEqual([line.begin for line in lines], [10, 15, 14.9])
        self.assertEqual([line.end for line in lines], [14, 20, 19])
        self.assertEqual(report["matched_reference_character_coverage"], 1)
        self.assertEqual(report["raw_transcript_text_similarity"], 1)

    def test_true_within_voice_reversal_and_legacy_reversal_still_fail_preflight(self):
        for remove_agents in (False, True):
            payload = self.payload()
            if remove_agents:
                for row in payload["reference_lines"]:
                    row.pop("agent")
            else:
                payload["reference_lines"][-1]["agent"] = "v1"
            with self.subTest(legacy=remove_agents), self.assertRaisesRegex(
                    RuntimeError, "reference windows are invalid: line 3 begin"):
                build_with_reference_retry({}, payload, lambda *_: self.fail("model started"))

    def test_missing_lrc_end_is_resolved_from_next_row_of_same_voice(self):
        payload = payload_for([
            {"text": "前句", "begin": 10, "end": None, "agent": "v1", "key": "L1"},
            {"text": "叠唱", "begin": 9, "end": None, "agent": "v2", "key": "L2"},
            {"text": "后句", "begin": 18, "end": None, "agent": "v1", "key": "L3"},
        ])
        rows = _reference_lines(payload, 30)
        self.assertEqual([row["end"] for row in rows], [18, 30, 30])
        self.assertEqual([row["key"] for row in rows], ["L1", "L2", "L3"])
        calls = []
        def transcribe(begin, end, text, language):
            calls.append((begin, end))
            return observation(text, 1.25, 2.25)
        ttml, report = build_with_reference_retry({}, payload, transcribe)
        self.assertEqual(calls, [(8.75, 18.75), (7.75, 30), (16.75, 30)])
        self.assertEqual([row.key for row in parse_word_ttml(ttml)], ["L1", "L2", "L3"])
        self.assertEqual(report["retried_windows"], 3)

    def test_missing_english_evidence_does_not_pass_existing_quality_gates(self):
        payload = self.payload()
        windows = [observation(row["text"], row["begin"], row["end"])
                   for row in payload["reference_lines"]]
        windows[-1] = {"segments": []}
        with self.assertRaisesRegex(RuntimeError, "acoustic/reference mismatch"):
            build_candidate({}, payload, reference_results=windows)
        calls = []
        def transcribe(begin, end, text, language):
            calls.append(text)
            return {"segments": []} if text == "English backing" else observation(text, 1.25, 2.25)
        with self.assertRaisesRegex(RuntimeError, "acoustic/reference mismatch"):
            build_with_reference_retry({}, payload, transcribe)
        self.assertEqual(calls[-1], "English backing")

    def test_agent_and_key_metadata_cannot_bypass_preflight_types(self):
        for field, value in [("agent", []), ("agent", ""), ("agent", "\x00"),
                             ("key", {}), ("key", "L1"), ("metadata", 1)]:
            payload = self.payload()
            payload["reference_lines"][-1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RuntimeError):
                validate_reference_payload(payload)

    def test_all_70_native_intervals_keep_order_keys_and_agents(self):
        # Text, voices and metadata are authored solely for this regression.
        fixture = Path(__file__).with_name("synthetic-parallel-70.ttml").read_text()
        rows = [dict(text=reference.text, begin=reference.begin, end=reference.end,
                     key=reference.key, agent=reference.agent, metadata=reference.metadata)
                for reference in parse_line_references(fixture)]
        metadata = rows[0]["metadata"]
        self.assertEqual(sum(any("a" <= c.casefold() <= "z" for c in row["text"])
                             for row in rows), 16)
        self.assertEqual(len(rows), 70)
        payload = payload_for(rows, duration=240)
        windows = [observation(row["text"], row["begin"], row["end"]) for row in rows]
        ttml, report = build_candidate({}, payload, reference_results=windows)
        lines = parse_word_ttml(ttml)
        self.assertEqual(len(lines), 70)
        for line, row in zip(lines, rows, strict=True):
            self.assertEqual((line.text, line.key, line.agent), (row["text"], row["key"], row["agent"]))
            self.assertEqual(line.metadata, metadata)
            self.assertAlmostEqual(line.begin, row["begin"], places=3)
            self.assertAlmostEqual(line.end, row["end"], places=3)
        self.assertEqual(report["matched_reference_character_coverage"], 1)
        self.assertEqual(report["raw_transcript_text_similarity"], 1)

    def test_bilingual_crops_do_not_inherit_whole_song_chinese_language(self):
        payload = self.payload()
        calls = []
        def transcribe(begin, end, text, language):
            calls.append(language)
            return observation(text, 1.25, 2.25)
        _, report = build_with_reference_retry({"language": "zh"}, payload, transcribe)
        self.assertEqual(calls, ["zh", "zh", "auto"])
        self.assertEqual(report["reference_window_languages"], calls)

    def test_window_language_preserves_override_and_legacy_single_language_hints(self):
        for texts, detected, expected in [
                (["中文", "汉字"], "zh", ["zh", "zh"]),
                (["bonjour", "monde"], "fr", ["fr", "fr"]),
                (["あさのうた", "よるのうた"], "zh", ["ja", "ja"]),
                (["中文 English", "あさのうた", "Bonjour", "Привет"], "zh",
                 ["auto", "ja", "auto", "auto"])]:
            payload = payload_for([{"text": text} for text in texts])
            with self.subTest(texts=texts):
                self.assertEqual(reference_window_languages(payload, detected), expected)
        for override in ("en", "auto", "zh"):
            payload = self.payload()
            payload["alignment_language"] = override
            self.assertEqual(reference_window_languages(payload, "ja"), [override] * 3)

    def test_parallel_echo_cannot_borrow_one_acoustic_clock_across_voices(self):
        # Original five-character echo with overlapping vocal-agent windows.
        rows = [
            {"text": "星舟穿雾来", "begin": 112.011, "end": 114.344, "key": "L36", "agent": "v1"},
            {"text": "星舟穿雾来", "begin": 113.712, "end": 116.006, "key": "L37", "agent": "v2000"},
        ]
        payload = payload_for(rows, duration=240)
        shared_observation = observation(rows[0]["text"], 113.2, 114.2)
        with self.assertRaisesRegex(RuntimeError, "require independent line windows"):
            build_candidate(shared_observation, payload)
        with self.assertRaisesRegex(RuntimeError, "native_anchor_fraction|share one acoustic clock"):
            build_candidate({}, payload, reference_results=[shared_observation, shared_observation])
        # An empty second crop cannot be rescued by lending it the same
        # whole-song phrase already assigned to the first vocal agent.
        with self.assertRaisesRegex(RuntimeError, "acoustic/reference mismatch"):
            build_candidate(shared_observation, payload,
                            reference_results=[shared_observation, {"segments": []}])
        independent = [observation(row["text"], row["begin"], row["end"]) for row in rows]
        ttml, report = build_candidate({}, payload, reference_results=independent)
        self.assertEqual([line.begin for line in parse_word_ttml(ttml)], [112.011, 113.712])
        self.assertEqual(report["matched_reference_character_coverage"], 1)

    def test_parallel_initial_candidate_always_runs_independent_crop_retry(self):
        payload = self.payload()
        calls = []
        def transcribe(begin, end, text, language):
            calls.append(text)
            reference = next(row for row in payload["reference_lines"] if row["text"] == text)
            return observation(text, reference["begin"] - begin, reference["end"] - begin)
        whole = {"segments": [{"words": [word for row in payload["reference_lines"]
                                         for word in observation(row["text"], row["begin"], row["end"])
                                         ["segments"][0]["words"]]}]}
        _, report = build_with_reference_retry(whole, payload, transcribe)
        self.assertEqual(calls, [row["text"] for row in payload["reference_lines"]])
        self.assertEqual(report["alignment_input_scope"], "per_reference_window")

    def test_repeated_echo_shifted_161ms_still_requires_native_acoustic_anchors(self):
        rows = [
            {"text": "星舟穿雾来", "begin": 112.011, "end": 114.344, "key": "L36", "agent": "v1"},
            {"text": "星舟穿雾来", "begin": 113.712, "end": 116.006, "key": "L37", "agent": "v2000"},
        ]
        payload = payload_for(rows, duration=240)
        # Four real matched-character spans in each crop, with the same
        # lead entry reheard 161 ms later by the second DTW observation.
        first = observation("星舟穿雾", 112.301, 113.741)
        repeated = observation("星舟穿雾", 112.462, 113.722)
        with self.assertRaisesRegex(RuntimeError, "L37 native_anchor_fraction=0.008 below 0.500"):
            build_candidate({}, payload, reference_results=[first, repeated])
        independent = [observation(row["text"], row["begin"], row["end"]) for row in rows]
        _, report = build_candidate({}, payload, reference_results=independent)
        self.assertEqual(report["repeated_parallel_native_anchor_fractions"], {"L36": 1, "L37": 1})
        self.assertEqual(reference_crop_bounds(rows, 0, payload["track"]["duration"]), (111.911, 115.094))
        self.assertEqual(tuple(round(value, 3) for value in reference_crop_bounds(rows, 1, payload["track"]["duration"])),
                         (113.612, 116.756))
        calls = []
        def transcribe(begin, end, text, language):
            calls.append((begin, end))
            row = rows[len(calls) - 1]
            return observation(text, row["begin"] - begin, row["end"] - begin)
        build_with_reference_retry({}, payload, transcribe)
        self.assertEqual([tuple(round(value, 3) for value in bounds) for bounds in calls],
                         [(111.911, 115.094), (113.612, 116.756)])

    def test_explicit_row_language_precedes_script_and_detected_hints(self):
        payload = self.payload()
        payload["reference_lines"][-1]["alignment_language"] = "en"
        self.assertEqual(reference_window_languages(payload, "zh"), ["zh", "zh", "en"])
        payload["alignment_language"] = "ja"
        self.assertEqual(reference_window_languages(payload, "zh"), ["ja"] * 3)
        for invalid in (None, 1, "english", "EN", ""):
            payload["reference_lines"][-1]["alignment_language"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(RuntimeError, "alignment_language"):
                build_with_reference_retry({}, payload, lambda *_: self.fail("model invoked"))

    def test_native_anchor_gate_accepts_50_percent_and_rejects_less(self):
        rows = [{"text": "回声", "begin": 10, "end": 12, "key": "L1", "agent": "v1"},
                {"text": "回声", "begin": 11, "end": 13, "key": "L2", "agent": "v2"}]
        payload = payload_for(rows)
        first = observation("回声", 10, 12)
        _, report = build_candidate({}, payload, reference_results=[first, observation("回声", 10.5, 11.5)])
        self.assertEqual(report["repeated_parallel_native_anchor_fractions"]["L2"], .5)
        with self.assertRaisesRegex(RuntimeError, "L2 native_anchor_fraction=0.490 below 0.500"):
            build_candidate({}, payload, reference_results=[first, observation("回声", 10.49, 11.49)])


class ParallelRetryCheckpointTests(unittest.TestCase):
    def test_equal_parallel_text_and_start_have_distinct_stable_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "song.wav"
            with wave.open(str(audio), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b"\0\0" * 10 * 16000)
            model = root / "model.bin"
            model.write_bytes(model_bytes())
            binary = root / "whisper-cli"
            binary.write_bytes(b"test binary")
            payload = payload_for([
                {"text": "同声", "begin": 2, "end": 4, "agent": "v1", "key": "L1"},
                {"text": "同声", "begin": 2, "end": 4, "agent": "v2", "key": "L2"},
            ], duration=10)
            calls = []
            def prepare(arguments, **kwargs):
                shutil.copyfile(audio, arguments[-1])
            def infer(arguments, **kwargs):
                prefix = Path(arguments[arguments.index("-of") + 1])
                calls.append(prefix.name)
                document = dtw_document("不符" if prefix.name == "whisper-result" else "同声",
                                        language="zh", start_ms=1250, step_ms=400)
                prefix.with_suffix(".json").write_text(json.dumps(document))
            def run():
                with patch("word_lyrics_automation.alignment_backend_whispercpp.subprocess.run", side_effect=prepare), \
                     patch("word_lyrics_automation.alignment_backend_whispercpp._run_with_progress", side_effect=infer):
                    return run_alignment(audio=audio, payload=payload, output=root / "out.ttml",
                                         report_path=root / "report.json", binary=binary, model=model,
                                         work_dir=root / "work", threads=1, timeout_seconds=300)
            report = run()
            self.assertEqual(calls, ["line-1", "line-2"])
            self.assertTrue(report["whole_song_inference_skipped"])
            self.assertEqual(report["retried_windows"], 2)
            self.assertTrue((root / "work/line-1.checkpoint.json").is_file())
            self.assertTrue((root / "work/line-2.checkpoint.json").is_file())
            self.assertEqual([line.key for line in parse_word_ttml((root / "out.ttml").read_text())],
                             ["L1", "L2"])
            calls.clear()
            resumed = run()
            self.assertEqual(calls, [])
            self.assertEqual(resumed["reused_acoustic_units"], 2)


if __name__ == "__main__":
    unittest.main()
