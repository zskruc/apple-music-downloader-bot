from __future__ import annotations

import copy
import unittest

from word_lyrics_automation.whisper_dtw import convert_dtw_document


def token(text: str, anchor: float, token_id: int = 100, **extra: object) -> dict:
    # Deliberately contradictory legacy offsets must never influence DTW.
    return {
        "text": text, "id": token_id, "t_dtw": anchor, "p": 0.8,
        "offsets": {"from": 0, "to": 999999}, **extra,
    }


def document(tokens: list[dict], text: str | None = None) -> dict:
    segment: dict = {"tokens": tokens}
    if text is not None:
        segment["text"] = text
    return {
        "model": {"multilingual": True}, "result": {"language": "yue"},
        "transcription": [segment],
    }


def fragment(payload: bytes) -> str:
    return payload.decode("utf-8", errors="surrogateescape")


class WhisperDtwTests(unittest.TestCase):
    def test_uses_dtw_ticks_and_keeps_final_lexical_group_untimed(self) -> None:
        result = convert_dtw_document(document([
            token("甲", 126), token("乙", 214), token("丙", 254),
        ], "甲乙丙"))
        self.assertEqual(result["language"], "yue")
        segment = result["segments"][0]
        self.assertEqual(segment["text"], "甲乙丙")
        self.assertEqual(segment["untimed_text"], "丙")
        self.assertEqual(segment["words"], [
            {"word": "甲", "start": 1.26, "end": 2.14, "probability": 0.8},
            {"word": "乙", "start": 2.14, "end": 2.54, "probability": 0.8},
        ])
        diag = result["clock_diagnostics"]
        self.assertEqual(diag["method"], "whisper_dtw_group_boundaries_v1")
        self.assertIs(diag["derived_group_end"], True)
        self.assertEqual(diag["timed_token_count"], 2)
        self.assertEqual(diag["untimed_token_count"], 1)
        self.assertEqual(diag["timed_character_count"], 2)
        self.assertEqual(diag["untimed_character_count"], 1)

    def test_same_anchor_multichar_group_is_not_split_or_given_fake_duration(self) -> None:
        result = convert_dtw_document(document([
            token("甲", 100, p=0.9), token("乙丙", 100, p=0.7), token("丁", 200),
        ]))
        self.assertEqual(result["segments"][0]["words"], [
            {"word": "甲乙丙", "start": 1.0, "end": 2.0, "probability": 0.8},
        ])
        self.assertEqual(result["clock_diagnostics"]["timed_unit_count"], 1)
        self.assertEqual(result["clock_diagnostics"]["timed_token_count"], 2)

    def test_saturated_final_nine_characters_remain_unknown_together(self) -> None:
        tail = "乙丙丁戊己庚辛壬癸"
        result = convert_dtw_document(document([
            token("甲", 100), *[token(character, 200) for character in tail],
        ], "甲" + tail))
        self.assertEqual(result["segments"][0]["untimed_text"], tail)
        self.assertEqual([word["word"] for word in result["segments"][0]["words"]], ["甲"])
        self.assertEqual(result["clock_diagnostics"]["untimed_character_count"], 9)

    def test_punctuation_and_whitespace_set_boundaries_before_filtering(self) -> None:
        result = convert_dtw_document(document([
            token("甲", 100), token("，", 120), token(" ", 500), token("乙", 900),
        ], "甲， 乙"))
        segment = result["segments"][0]
        self.assertEqual(segment["words"][0]["end"], 1.2)
        self.assertEqual(len(segment["words"]), 1)
        self.assertEqual(segment["text"], "甲， 乙")
        self.assertEqual(segment["untimed_text"], "乙")
        self.assertEqual(result["clock_diagnostics"]["raw_text_token_count"], 4)
        self.assertEqual(result["clock_diagnostics"]["timed_token_count"], 3)

    def test_control_tokens_do_not_supply_a_final_end(self) -> None:
        result = convert_dtw_document(document([
            token("[_BEG_]", -1, 50365), token("甲", 100), token("乙", 200),
            token("[_TT_483]", 900, 50848),
            token("<|endoftext|>", -1, 50257),
        ]))
        self.assertEqual(result["segments"][0]["text"], "甲乙")
        self.assertEqual(result["segments"][0]["untimed_text"], "乙")
        self.assertEqual(result["clock_diagnostics"]["raw_text_token_count"], 2)

    def test_end_token_boundary_depends_on_multilingual_flag(self) -> None:
        value = document([token("甲", 100), token("乙", 200, 50256)])
        self.assertEqual(convert_dtw_document(value)["segments"][0]["untimed_text"], "乙")
        value["model"]["multilingual"] = False
        with self.assertRaisesRegex(RuntimeError, "no timed acoustic tokens"):
            convert_dtw_document(value)

    def test_numeric_id_is_authoritative_even_for_control_looking_text(self) -> None:
        result = convert_dtw_document(document([
            token("<|literal|>", 100), token("乙", 200),
        ]))
        self.assertEqual(result["segments"][0]["words"][0]["word"], "<|literal|>")

    def test_utf8_fragments_across_known_groups_form_one_complete_unit(self) -> None:
        encoded = "优".encode()
        result = convert_dtw_document(document([
            token(fragment(encoded[:2]), 100), token(fragment(encoded[2:]), 120),
            token("甲", 200), token("乙", 300),
        ], "优甲乙"))
        segment = result["segments"][0]
        self.assertEqual([word["word"] for word in segment["words"]], ["优", "甲"])
        self.assertEqual(segment["words"][0]["start"], 1.0)
        self.assertEqual(segment["words"][0]["end"], 2.0)
        self.assertEqual(segment["untimed_text"], "乙")

    def test_utf8_unit_touching_final_unknown_group_is_wholly_untimed(self) -> None:
        encoded = "优".encode()
        result = convert_dtw_document(document([
            token("甲", 50), token("乙" + fragment(encoded[:2]), 100),
            token(fragment(encoded[2:]) + "丙", 120),
        ], "甲乙优丙"))
        segment = result["segments"][0]
        self.assertEqual([word["word"] for word in segment["words"]], ["甲"])
        self.assertEqual(segment["untimed_text"], "乙优丙")
        self.assertEqual(segment["text"], "甲乙优丙")
        diag = result["clock_diagnostics"]
        self.assertEqual(diag["timed_token_count"], 1)
        self.assertEqual(diag["untimed_token_count"], 2)
        self.assertEqual(diag["untimed_character_count"], 3)

    def test_utf8_fragments_at_same_anchor_decode_without_replacement(self) -> None:
        encoded = "优".encode()
        result = convert_dtw_document(document([
            token(fragment(encoded[:2]), 100), token(fragment(encoded[2:]), 100),
            token("乙", 200),
        ]))
        self.assertEqual(result["segments"][0]["text"], "优乙")
        self.assertEqual(result["segments"][0]["words"][0]["word"], "优")

    def test_final_punctuation_boundary_can_time_preceding_text(self) -> None:
        result = convert_dtw_document(document([token("甲", 100), token("。", 200)]))
        self.assertEqual(result["segments"][0]["words"][0]["end"], 2.0)
        self.assertEqual(result["segments"][0]["untimed_text"], "。")
        self.assertEqual(result["clock_diagnostics"]["untimed_character_count"], 0)

    def test_empty_text_token_still_sets_a_boundary(self) -> None:
        result = convert_dtw_document(document([
            token("甲", 100), token("", 120), token("乙", 200),
        ]))
        self.assertEqual(result["segments"][0]["words"][0]["end"], 1.2)

    def test_does_not_bridge_raw_segments_or_use_segment_end(self) -> None:
        value = document([token("甲", 100), token("乙", 200)], "甲乙")
        value["transcription"][0]["offsets"] = {"from": 0, "to": 999999}
        value["transcription"].append({
            "text": "丙丁", "tokens": [token("丙", 500), token("丁", 600)],
        })
        result = convert_dtw_document(value)
        self.assertEqual([segment["untimed_text"] for segment in result["segments"]], ["乙", "丁"])
        self.assertEqual([len(segment["words"]) for segment in result["segments"]], [1, 1])

    def test_split_utf8_cannot_be_completed_by_a_different_raw_segment(self) -> None:
        encoded = "优".encode()
        value = document([token("甲", 50), token(fragment(encoded[:2]), 100)])
        value["transcription"].append({
            "tokens": [token(fragment(encoded[2:]), 200), token("乙", 300)],
        })
        with self.assertRaisesRegex(RuntimeError, "split UTF-8"):
            convert_dtw_document(value)

    def test_original_segment_text_is_preserved_verbatim(self) -> None:
        result = convert_dtw_document(document([
            token(" 甲", 100), token("乙！ ", 200),
        ], " 甲乙！ "))
        self.assertEqual(result["segments"][0]["text"], " 甲乙！ ")
        self.assertEqual(result["segments"][0]["words"][0]["word"], " 甲")
        self.assertEqual(result["segments"][0]["untimed_text"], "乙！ ")

    def test_requires_real_model_multilingual_boolean(self) -> None:
        base = document([token("甲", 100), token("乙", 200)])
        for model in (None, {}, {"multilingual": "true"}, {"multilingual": 1}):
            with self.subTest(model=model), self.assertRaisesRegex(RuntimeError, "model.multilingual"):
                value = copy.deepcopy(base)
                value["model"] = model
                convert_dtw_document(value)

    def test_invalid_dtw_rejects_even_when_legacy_offsets_are_positive(self) -> None:
        for anchor in (None, -1, float("nan"), float("inf"), "100", True):
            with self.subTest(anchor=anchor), self.assertRaisesRegex(RuntimeError, "DTW"):
                convert_dtw_document(document([token("甲", anchor), token("乙", 200)]))
        with self.assertRaisesRegex(RuntimeError, "non-monotonic"):
            convert_dtw_document(document([token("甲", 200), token("，", 100), token("乙", 300)]))

    def test_unknown_only_and_missing_dtw_do_not_fall_back_to_offsets(self) -> None:
        examples = [
            document([token("甲", 100)]),
            document([token("甲", 100), token("乙", 100)]),
            document([token("[_BEG_]", -1, 50365)]),
            document([token("，", 100), token("乙", 200)]),
            document([]),
        ]
        for value in examples:
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "no timed acoustic tokens"):
                convert_dtw_document(value)
        value = document([token("甲", 100), token("乙", 200)])
        del value["transcription"][0]["tokens"][0]["t_dtw"]
        with self.assertRaisesRegex(RuntimeError, "t_dtw"):
            convert_dtw_document(value)

    def test_invalid_ids_and_utf8_fail_closed(self) -> None:
        for token_id in (None, -1, True, "100", 1.5):
            with self.subTest(token_id=token_id), self.assertRaisesRegex(RuntimeError, "token id"):
                convert_dtw_document(document([token("甲", 100, token_id), token("乙", 200)]))
        for broken in (b"\xff", b"\xe4\xbc"):
            with self.subTest(broken=broken), self.assertRaisesRegex(RuntimeError, "UTF-8"):
                convert_dtw_document(document([token("甲", 50), token(fragment(broken), 100)]))
        with self.assertRaisesRegex(RuntimeError, "surrogate"):
            convert_dtw_document(document([token("\ud800", 100), token("乙", 200)]))

    def test_probabilities_are_finite_and_bounded(self) -> None:
        for probability in (float("nan"), float("inf"), -0.1, 1.1, True):
            with self.subTest(probability=probability), self.assertRaisesRegex(RuntimeError, "probability"):
                convert_dtw_document(document([token("甲", 100, p=probability), token("乙", 200)]))


if __name__ == "__main__":
    unittest.main()
