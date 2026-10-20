from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from word_lyrics_automation.instrumental import confirm_catalog_instrumental, netease_instrumental
from word_lyrics_automation.providers.musiksammler import get_record, parse_release
from word_lyrics_automation.providers.netease import NeteaseSong
from word_lyrics_automation.resolver import CandidateRejected, LegacyGateway
from word_lyrics_automation.fingerprint import media_fingerprint


TRACK = SimpleNamespace(title="Serenata", album="Love in Venice",
    artist="安德列・里欧 & Johann Strauss Orchestra", duration=328.3066666666667)
NAMES = ["André Rieu", "Johann Strauss Orchestra"]
SONG = NeteaseSong("2632199063", "Serenata", "André Rieu / Johann Strauss Orchestra", "Love in Venice", 328306)
LYRIC = {"pureMusic": True, "code": 200, "lrc": {"lyric": "[00:00.00] 作曲 : Test\n[00:05.00]纯音乐，请欣赏\n"}}
URL = "https://www.musik-sammler.de/release/1091976/"
HTML = '''<h1><a href="/artist/42/">André Rieu</a> Love In Venice <small>CD, 2014</small></h1>
<p>I = Instrumental</p><table><tr><td>16.</td><td>Serenata</td><td>5:28</td><td>I</td></tr></table>'''


class CatalogInstrumentalTests(unittest.TestCase):
    def record(self, html=HTML):
        return parse_release(html, title=TRACK.title, album=TRACK.album, artist="André Rieu", url=URL)

    def test_two_explicit_sources_match_full_recording_signature(self):
        evidence = confirm_catalog_instrumental(TRACK, NAMES, SONG, LYRIC, self.record())
        self.assertEqual(evidence["independent_sources"], 2)
        self.assertEqual(evidence["sources"][0]["id"], SONG.song_id)
        self.assertEqual(evidence["sources"][1]["track_number"], 16)

    def test_empty_lyrics_or_instrumental_album_label_are_not_positive_evidence(self):
        self.assertFalse(netease_instrumental({"lrc": {"lyric": ""}}))
        self.assertFalse(netease_instrumental({"pureMusic": "true"}))
        record = self.record(HTML.replace("<td>I</td>", "<td></td>"))
        self.assertFalse(record["instrumental"])
        self.assertIsNone(confirm_catalog_instrumental(TRACK, NAMES, SONG, LYRIC, record))

    def test_contradictory_real_lyrics_veto_instrumental_flag(self):
        for field, text in [("lrc", "[00:01.00]These are sung words"),
                            ("yrc", "[1000,1000](1000,1000,0)words"),
                            ("tlyric", "[00:01.00]Translated sung words")]:
            with self.subTest(field=field):
                bad = copy.deepcopy(LYRIC);bad[field] = {"lyric": text}
                self.assertFalse(netease_instrumental(bad))

    def test_different_version_artist_album_or_duration_is_rejected(self):
        for fields in [{"title": "Serenata (Live)"}, {"album": "Another Album"},
                       {"artist": "Another Rieu / Johann Strauss Orchestra"},
                       {"artist": "Johann Strauss Orchestra"}, {"duration_ms": 327000}]:
            with self.subTest(fields=fields):
                song = NeteaseSong(**{**{k: getattr(SONG, k) for k in SONG.__dataclass_fields__}, **fields})
                self.assertIsNone(confirm_catalog_instrumental(TRACK, NAMES, song, LYRIC, self.record()))
        for fields in [{"title": "Serenata (Live)"}, {"artist": "Other Artist"},
                       {"album": "Other Album"}, {"duration": 327},
                       {"duration": float("nan")}, {"url": "https://example.com/release/1/"}]:
            with self.subTest(fields=fields):
                self.assertIsNone(confirm_catalog_instrumental(TRACK, NAMES, SONG, LYRIC, {**self.record(), **fields}))

    def test_native_search_reads_a_release_and_layout_changes_fail_closed(self):
        with patch("word_lyrics_automation.providers.musiksammler._request_html",
                   side_effect=['<a href="/release/1091976/">record</a>', HTML]) as request:
            result = get_record(title=TRACK.title, album=TRACK.album, artist="André Rieu")
            self.assertTrue(result["instrumental"])
            self.assertIn("query=", request.call_args_list[0].args[0])
        self.assertIsNone(self.record(HTML.replace('<td>16.</td>', '<td>unknown</td>')))
        self.assertIsNone(self.record(HTML.replace('Love In Venice', 'Different album')))
        absolute = HTML.replace('href="/artist/42/"', 'href="https://www.musik-sammler.de/artist/andre-rieu/"')
        self.assertTrue(self.record(absolute)["instrumental"])
        self.assertIsNone(self.record(absolute.replace('https://www.musik-sammler.de/artist/', 'https://example.com/artist/')))

    def gateway(self, root):
        gateway = object.__new__(LegacyGateway);gateway.state_dir = root
        gateway.references = lambda *_: [];gateway.existing_word = lambda *_: None
        gateway.legacy = SimpleNamespace(Result=SimpleNamespace)
        def write(media, text, suffix):
            target = media.with_suffix(suffix);target.write_text(text);return str(target), ""
        gateway.service = SimpleNamespace(write_text_sidecar=write, audit=SimpleNamespace(record=lambda _: None))
        return gateway

    def test_alternative_evidence_is_used_even_when_other_databases_are_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);media = root / "song.m4a";media.write_bytes(b"immutable audio")
            track = SimpleNamespace(**vars(TRACK), path=media, isrc="DEUM71403281", song_id="test")
            gateway = self.gateway(root);before = media_fingerprint(media)
            def unavailable(**_):raise OSError("source offline")
            with patch.dict("sys.modules", {"mutagen": SimpleNamespace(File=lambda _: SimpleNamespace(tags={}))}):
                result = gateway.instrumental_result(track, before, NAMES,
                    netease_records=[(SONG, LYRIC)], catalog_fetch_function=lambda **_: self.record(),
                    lrclib_fetch_function=unavailable)
            self.assertEqual(result["provider"], "netease_musiksammler")
            self.assertEqual(media_fingerprint(media), before)
            self.assertEqual(result["metrics"]["timed_words"], 0)

    def test_audio_changed_during_external_verification_is_rejected_before_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);media = root / "song.m4a";media.write_bytes(b"original audio")
            track = SimpleNamespace(**vars(TRACK), path=media, isrc="DEUM71403281", song_id="test")
            gateway = self.gateway(root);before = media_fingerprint(media)
            def changing_source(**_):media.write_bytes(b"new audio");return self.record()
            with patch.dict("sys.modules", {"mutagen": SimpleNamespace(File=lambda _: SimpleNamespace(tags={}))}):
                with self.assertRaises(CandidateRejected):
                    gateway.instrumental_result(track, before, NAMES, netease_records=[(SONG, LYRIC)],
                        catalog_fetch_function=changing_source, lrclib_fetch_function=lambda **_: None)
            self.assertFalse(media.with_suffix(".lrc").exists())


if __name__ == "__main__":
    unittest.main()
