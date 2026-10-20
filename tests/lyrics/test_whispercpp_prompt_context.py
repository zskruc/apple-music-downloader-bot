from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from word_lyrics_automation.alignment_backend_whispercpp import run_alignment
from whispercpp_test_fixtures import model_bytes


class WhisperCppPromptContextTests(unittest.TestCase):
    """Check actual subprocess argv; fixture output never derives from prompt."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.audio = self.root / "audio.wav"
        with wave.open(str(self.audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\0\0" * 30 * 16000)
        self.model = self.root / "model.bin"
        self.model.write_bytes(model_bytes(suffix=b"fixture model identity"))
        self.log = self.root / "actual-argv.jsonl"
        self.binary = self.root / "whisper-cli-fixture"
        # This real child process records the received argv and writes a fixed
        # canned document per unit. It is not an ASR quality test and never
        # reads --prompt to choose its output (unlike earlier retry mocks).
        self.binary.write_text(f"#!{sys.executable}\n" + r'''
import json
import sys
from pathlib import Path

arguments = sys.argv
with Path(LOG_PATH).open("a") as handle:
    handle.write(json.dumps(arguments) + "\n")
prefix = Path(arguments[arguments.index("-of") + 1])
text = {"whisper-result": "wrong", "line-1": "あさのうた", "line-2": "よるのうた"}[prefix.name]
tokens = [{"text": character, "id": 1000 + index, "t_dtw": 125 + index * 70,
           "offsets": {"from": 1250 + index * 700,
           "to": 1950 + index * 700}, "p": .9} for index, character in enumerate(text)]
tokens.append({"text": "。", "id": 1543, "t_dtw": 125 + len(text) * 70, "p": .9})
prefix.with_suffix(".json").write_text(json.dumps({
    "model": {"multilingual": True, "vocab": 51866, "mels": 128,
              "audio": {"ctx": 1500, "state": 1280, "head": 20, "layer": 32},
              "text": {"ctx": 448, "state": 1280, "head": 20, "layer": 4}},
    "result": {"language": "ja"}, "transcription": [{"tokens": tokens}]}))
'''.replace("LOG_PATH", repr(str(self.log))))
        self.binary.chmod(0o755)
        self.payload = {
            "track": {"duration": 30}, "reference_text": "あさのうたよるのうた",
            "reference_lines": [{"text": "あさのうた", "begin": 10, "end": 15},
                                {"text": "よるのうた", "begin": 20, "end": 25}],
        }

    def arguments(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def align(self):
        def prepare(arguments, **_kwargs):
            shutil.copyfile(self.audio, arguments[-1])

        # Only decoded-audio preparation is replaced. Inference uses the real
        # _run_with_progress -> Popen path into the argv-recording executable.
        with patch("word_lyrics_automation.alignment_backend_whispercpp.subprocess.run", side_effect=prepare):
            return run_alignment(audio=self.audio, payload=self.payload,
                output=self.root / "candidate.ttml", report_path=self.root / "report.json",
                binary=self.binary, model=self.model, work_dir=self.root / "work",
                threads=1, timeout_seconds=60)

    def test_real_whole_and_crop_processes_receive_enabled_bounded_prompt_context(self):
        report = self.align()
        arguments = self.arguments()
        self.assertEqual(len(arguments), 3)
        self.assertEqual([argv[argv.index("--prompt") + 1] for argv in arguments],
            [self.payload["reference_text"], "あさのうた", "よるのうた"])
        for argv in arguments:
            self.assertEqual(argv[argv.index("-mc") + 1], "128")
            self.assertEqual(argv.count("-mc"), 1)
            self.assertNotIn("-nc", argv)
            self.assertNotIn("--no-context", argv)
            self.assertNotIn("--carry-initial-prompt", argv)
        self.assertEqual(report["retried_windows"], 2)
        self.assertEqual(report["cue_count"], 10)

    def test_no_reference_process_retains_zero_context_and_no_prompt(self):
        self.payload.update(reference_text="", reference_lines=[])
        with self.assertRaisesRegex(RuntimeError, "trusted reference text is required"):
            self.align()
        arguments = self.arguments()
        self.assertEqual(len(arguments), 1)
        self.assertEqual(arguments[0][arguments[0].index("-mc") + 1], "0")
        self.assertNotIn("--prompt", arguments[0])
        self.assertNotIn("-nc", arguments[0])

    def test_zero_context_checkpoints_miss_by_argv_without_changing_context_schema(self):
        self.align()
        recorded = self.arguments()
        context = json.loads((self.root / "work/checkpoint-context.json").read_text())
        self.assertEqual(context["version"], 2)
        context_key = context["key"]
        for argv in recorded:
            legacy_arguments = list(argv)
            legacy_arguments[legacy_arguments.index("-mc") + 1] = "0"
            prefix = Path(argv[argv.index("-of") + 1])
            unit = "whole-song" if prefix.name == "whisper-result" else prefix.name
            checkpoint = self.root / "work" / (unit + ".checkpoint.json")
            saved = json.loads(checkpoint.read_text())
            saved["key"] = hashlib.sha256(json.dumps(
                [context_key, legacy_arguments], sort_keys=True).encode()).hexdigest()
            checkpoint.write_text(json.dumps(saved))
        untouched = self.root / "work/unclaimed-history.checkpoint.json"
        untouched.write_text("retained legacy evidence")
        self.log.write_text("")
        report = self.align()
        self.assertEqual(len(self.arguments()), 3)
        self.assertEqual(report["reused_acoustic_units"], 0)
        self.assertEqual(json.loads((self.root / "work/checkpoint-context.json").read_text())["key"], context_key)
        self.assertEqual(untouched.read_text(), "retained legacy evidence")
        self.log.write_text("")
        resumed = self.align()
        self.assertEqual(self.arguments(), [])
        self.assertEqual(resumed["reused_acoustic_units"], 3)


if __name__ == "__main__":
    unittest.main()
