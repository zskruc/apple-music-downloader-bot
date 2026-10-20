from __future__ import annotations

import hashlib
import importlib.util
import sys
import unicodedata
import unittest
from pathlib import Path

from word_lyrics_automation._opencc_t2s import (
    EXCLUDED_ROWS,
    INCLUDED_ROWS,
    MAPPING_SHA256,
    TRADITIONAL_TO_SIMPLIFIED,
)
from word_lyrics_automation.alignment import build_candidate
from word_lyrics_automation.chinese_text import fold_chinese_script
from word_lyrics_automation.identity import artist_matches, title_matches
from word_lyrics_automation.text import display_units, normalize_text
from word_lyrics_automation.ttml import parse_word_ttml


class ChineseComparisonTests(unittest.TestCase):
    def test_safe_script_variants_match_without_losing_original_display(self) -> None:
        traditional = "風中的雲，愛與憂傷。"
        simplified = "风中的云，爱与忧伤。"
        self.assertEqual(normalize_text(traditional), normalize_text(simplified))
        self.assertEqual("".join(display_units(traditional)), traditional)
        self.assertEqual(fold_chinese_script("あさ ガラス ｶﾞﾗｽ"), "あさ ガラス ｶﾞﾗｽ")
        self.assertEqual(normalize_text("ｶﾞﾗｽ"), "ガラス")

    def test_script_matching_does_not_turn_homophones_or_versions_equivalent(self) -> None:
        self.assertNotEqual(normalize_text("孤獨"), normalize_text("辜毒"))
        self.assertNotEqual(normalize_text("愛的故事 (Live)"), normalize_text("爱的故事"))
        self.assertNotEqual(normalize_text("愛的故事 (Remix)"), normalize_text("爱的故事"))
        self.assertTrue(title_matches("愛的故事", "爱的故事"))
        self.assertTrue(artist_matches("陳奕迅", "陈奕迅"))

    def test_context_dependent_dictionary_rows_are_not_guessed(self) -> None:
        # These source rows have multiple targets in the vendored dictionary.
        for character in "乾於開買著":
            with self.subTest(character=character):
                self.assertNotIn(ord(character), TRADITIONAL_TO_SIMPLIFIED)
                self.assertEqual(fold_chinese_script(character), character)

    def test_vendored_map_is_length_preserving_idempotent_and_traceable(self) -> None:
        self.assertEqual(len(TRADITIONAL_TO_SIMPLIFIED), INCLUDED_ROWS)
        self.assertEqual(INCLUDED_ROWS, 4057)
        self.assertEqual(EXCLUDED_ROWS, 56)
        pairs = "".join(chr(key) + value for key, value in sorted(TRADITIONAL_TO_SIMPLIFIED.items()))
        self.assertEqual(hashlib.sha256(pairs.encode()).hexdigest(), MAPPING_SHA256)
        for key, target in TRADITIONAL_TO_SIMPLIFIED.items():
            with self.subTest(character=chr(key)):
                self.assertEqual(len(target), 1)
                self.assertEqual(fold_chinese_script(target), target)

    def test_alignment_uses_script_equivalence_and_publishes_reference_original(self) -> None:
        reference = "風中的雲愛與憂傷"
        transcript = "风中的云爱与忧伤"
        result = {"language": "zh", "segments": [{"words": [
            {"word": character, "start": 1 + index * 0.4,
             "end": 1.3 + index * 0.4, "probability": 0.98}
            for index, character in enumerate(transcript)
        ]}]}
        payload = {
            "track": {"title": "Test", "artist": "Test", "duration": 10},
            "reference_text": reference,
            "reference_lines": [{"text": reference, "begin": 1, "end": 6}],
        }
        ttml, report = build_candidate(result, payload)
        self.assertEqual(report["matched_reference_character_coverage"], 1)
        self.assertEqual(report["raw_transcript_text_similarity"], 1)
        self.assertEqual(parse_word_ttml(ttml)[0].text, reference)

    def test_legacy_normalization_keeps_existing_alphanumeric_filter(self) -> None:
        source = Path(__file__).resolve().parents[2] / "bot/word_lyrics.py"
        if not source.is_file():
            source = Path(importlib.util.find_spec("word_lyrics").origin)
        module_name = "_chinese_text_test_word_lyrics"
        spec = importlib.util.spec_from_file_location(module_name, source)
        legacy = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = legacy
        try:
            spec.loader.exec_module(legacy)
            self.assertEqual(legacy.normalize_text("風與雲"), "风与云")
            for value in (None, 123, "ＡＢＣ，ガラス！", "x\u0301_1", "\u093f"):
                original = unicodedata.normalize("NFKC", str(value or "")).casefold()
                expected = "".join(character for character in original if character.isalnum())
                self.assertEqual(legacy.normalize_text(value), expected)
        finally:
            sys.modules.pop(module_name, None)


if __name__ == "__main__":
    unittest.main()
