from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from word_lyrics_automation.alignment_backend_whispercpp import run_alignment
from whispercpp_test_fixtures import dtw_document, model_bytes


class WhisperCppRetryTests(unittest.TestCase):
    def test_line_retry_crops_audio_and_keeps_absolute_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "input.wav"
            with wave.open(str(audio), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(16000)
                out.writeframes(b"\0\0" * (30 * 16000))
            payload = {
                "track": {"duration": 30, "title": "Test", "artist": "Artist"},
                "reference_text": "あさのうたよるのうた",
                "reference_lines": [{"text": "あさのうた", "begin": 10, "end": 15}, {"text": "よるのうた", "begin": 20, "end": 25}],
            }
            model_path = root / "fixture-model.bin"
            model_path.write_bytes(model_bytes())
            calls = []

            def prepare(arguments, **kwargs):
                shutil.copyfile(audio, arguments[-1])

            def model(arguments, **kwargs):
                prompt = arguments[arguments.index("--prompt") + 1]
                source = Path(arguments[arguments.index("-f") + 1])
                prefix = Path(arguments[arguments.index("-of") + 1])
                with wave.open(str(source), "rb") as inp:
                    calls.append(inp.getnframes() / inp.getframerate())
                text = "wrong" if len(calls) == 1 else prompt
                prefix.with_suffix(".json").write_text(json.dumps(dtw_document(text)))

            with patch("word_lyrics_automation.alignment_backend_whispercpp.subprocess.run", side_effect=prepare), patch("word_lyrics_automation.alignment_backend_whispercpp._run_with_progress", side_effect=model):
                report = run_alignment(audio=audio, payload=payload, output=root / "result.ttml", report_path=root / "report.json", binary=Path("/unused"), model=model_path, work_dir=root, threads=1, timeout_seconds=300)
            self.assertEqual(calls, [30, 7, 7])
            self.assertEqual(report["retried_windows"], 2)
            self.assertEqual(report["cue_count"], 10)
            self.assertEqual(report["alignment_engine"], "whisper.cpp")
