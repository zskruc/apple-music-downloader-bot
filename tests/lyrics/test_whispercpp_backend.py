from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from word_lyrics_automation.alignment_backend_whispercpp import (
    load_whispercpp_document,
    whispercpp_document_to_result,
)


class WhisperCppBackendTests(unittest.TestCase):
    def test_full_json_tokens_become_timed_acoustic_units(self) -> None:
        document = {
            "result": {"language": "zh"},
            "transcription": [
                {
                    "tokens": [
                        {
                            "text": "<|startoftranscript|>",
                            "offsets": {"from": 0, "to": 10},
                            "p": 1,
                        },
                        {
                            "text": " 红豆",
                            "offsets": {"from": 1000, "to": 1500},
                            "p": 0.95,
                        },
                        {
                            "text": "生南国",
                            "offsets": {"from": 1500, "to": 2300},
                            "p": 0.9,
                        },
                    ]
                }
            ],
        }
        result = whispercpp_document_to_result(document)
        self.assertEqual(result["language"], "zh")
        words = result["segments"][0]["words"]
        self.assertEqual([word["word"] for word in words], [" 红豆", "生南国"])
        self.assertEqual(words[0]["start"], 1.0)
        self.assertEqual(words[1]["end"], 2.3)

    def test_missing_token_timestamps_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no timed acoustic tokens"):
            whispercpp_document_to_result(
                {"transcription": [{"tokens": [{"text": "hello", "p": 0.9}]}]}
            )

    def test_split_utf8_token_bytes_are_reassembled_without_replacement(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "result.json"
            path.write_bytes(
                b'{"result":{"language":"zh"},"transcription":[{"tokens":['
                b'{"text":"\xe4\xbc","offsets":{"from":750,"to":750},"p":0.1},'
                b'{"text":"\x98","offsets":{"from":780,"to":1120},"p":0.9}'
                b"]}]}"
            )
            document = load_whispercpp_document(path)
            result = whispercpp_document_to_result(document)
            word = result["segments"][0]["words"][0]
            self.assertEqual(word["word"], "优")
            self.assertEqual(word["start"], 0.75)
            self.assertEqual(word["end"], 1.12)


if __name__ == "__main__":
    unittest.main()
