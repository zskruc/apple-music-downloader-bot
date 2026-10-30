import tempfile
import unittest
from pathlib import Path

from word_lyrics_automation.fingerprint import media_fingerprint
from word_lyrics_automation.player_verification import (PlayerVerificationError,
                                                       compare_word_lyrics, verify_publication)


TTML = '''<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata"><body><div><p begin="00:00:01.000" end="00:00:03.000" ttm:agent="v1"><span begin="00:00:01.000" end="00:00:02.000">甲</span><span begin="00:00:02.000" end="00:00:03.000">乙</span></p></div></body></tt>'''


def lyrics():
    return [{"kind": "main", "line": [{"value": "甲乙", "start": 1000}], "cueLine": [
        {"index": 0, "agentId": "v1", "value": "甲乙", "start": 1000, "end": 3000, "cue": [
            {"value": "甲", "start": 1000, "end": 2000, "byteStart": 0, "byteEnd": 2},
            {"value": "乙", "start": 2000, "end": 3000, "byteStart": 3, "byteEnd": 5}]}]}]


class PlayerVerificationTests(unittest.TestCase):
    def test_actual_player_text_clock_and_unicode_ranges_must_match(self):
        self.assertEqual(compare_word_lyrics(TTML, lyrics())["cues"], 2)
        response = lyrics(); response[0]["cueLine"][0]["cue"][1]["byteStart"] = 2
        with self.assertRaises(PlayerVerificationError):
            compare_word_lyrics(TTML, response)

    def test_missing_word_response_is_not_success_after_scan_acceptance(self):
        with self.assertRaises(PlayerVerificationError):
            compare_word_lyrics(TTML, [])

    def test_main_and_background_groups_are_preserved(self):
        raw = TTML.replace('</p>', '<span ttm:role="x-bg"><span begin="00:00:01.500" end="00:00:02.500">丙</span></span></p>')
        response = lyrics();response[0]["line"][0]["value"] = "甲乙丙"
        response[0]["cueLine"].append({"index":0,"agentId":"__nd_bg__|v1","value":"丙","start":1000,"end":3000,
            "cue":[{"value":"丙","start":1500,"end":2500,"byteStart":0,"byteEnd":2}]})
        self.assertEqual(compare_word_lyrics(raw,response)["cues"],3)
        response[0]["cueLine"][1]["agentId"]="v1"
        with self.assertRaises(PlayerVerificationError):compare_word_lyrics(raw,response)

    def test_same_title_old_mp3_cannot_acknowledge_new_flac(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            audio=Path(tmp)/'song.flac';audio.write_bytes(b'new audio')
            side=audio.with_suffix('.ttml');side.write_text(TTML)
            fingerprint=media_fingerprint(audio)
            result={"sidecar_path":str(side),"candidate_sha256":hashlib.sha256(side.read_bytes()).hexdigest()}
            calls=[]
            def request(method,**kw):
                calls.append(method)
                if method=='search3':return {'searchResult3':{'song':[{'id':'old','title':'曲','artist':'人','album':'集','size':4,'suffix':'mp3'}]}}
                return {'lyricsList':{'structuredLyrics':lyrics()}}
            with self.assertRaises(PlayerVerificationError) as caught:
                verify_publication(audio,{'title':'曲','artist':'人','album':'集'},result,fingerprint,request=request)
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(calls,['search3'])


if __name__ == '__main__':unittest.main()
