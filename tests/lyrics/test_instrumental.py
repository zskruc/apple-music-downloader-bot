from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from word_lyrics_automation.fingerprint import media_fingerprint
from word_lyrics_automation.instrumental import confirm_instrumental, is_instrumental_marker
from word_lyrics_automation.resolver import LegacyGateway, Resolution
from word_lyrics_automation.queue import QueueStore
from word_lyrics_automation.source_worker import SourceWorker


TRACK = SimpleNamespace(title="Epilogue", artist="贾斯汀 · 赫尔维茨",
    album="La La Land (Original Motion Picture Soundtrack)", duration=459.506667,
    song_id="1440864379", isrc="USUG11600658")
RECORD = {"id": 23361498, "trackName": "Epilogue", "artistName": "Justin Hurwitz",
    "albumName": TRACK.album, "duration": 459.473167, "instrumental": True,
    "plainLyrics": None, "syncedLyrics": None, "lyricsfile": "version: '1.0'\nlines: []\n"}
ISRC = {"isrc": TRACK.isrc, "recordings": [{"id": "5d6ce5ba-8390-4a1e-9433-97320855286d",
    "title": "Epilogue", "length": 459507, "artist-credit": [{"name": "Justin Hurwitz"}],
    "relations": [{"type": "performance", "target-type": "work", "attributes": [],
                   "work": {"id": "6b668904-1eae-466c-8b8d-dc5f9f35c294", "languages": ["zxx"]}}]}]}


class InstrumentalTests(unittest.TestCase):
    def confirm(self, record=None, isrc=None, names=None):
        return confirm_instrumental(TRACK, names if names is not None else ["Justin Hurwitz"],
            record if record is not None else RECORD, isrc if isrc is not None else ISRC)

    def test_exact_real_recording_requires_two_sources_and_verified_regional_name(self):
        result = self.confirm()
        self.assertEqual(result["classification"], "instrumental_or_wordless")
        self.assertEqual(result["duration_delta_seconds"], .0335)
        self.assertIsNone(self.confirm(names=[]))
        self.assertIsNone(self.confirm(isrc={}))

    def test_wrong_recording_and_conflicting_lyric_fields_are_rejected(self):
        for changes in ({"trackName": "Epilogue (Live)"}, {"artistName": "Justin Hurwitz Tribute"},
                        {"albumName": "Other Release"}, {"duration": 460.5}, {"duration": float("nan")},
                        {"instrumental": False}, {"instrumental": "true"}, {"id": None},
                        {"plainLyrics": "Sung words"}, {"syncedLyrics": "[00:01]Sung words"},
                        {"lyricsfile": "lines:\n- text: Sung words\n"}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.confirm(record={**RECORD, **changes}))

    def test_isrc_identity_and_every_work_must_confirm_no_language(self):
        for change in ("isrc", "title", "artist", "duration", "language", "unknown", "second_work"):
            data = copy.deepcopy(ISRC)
            recording = data["recordings"][0]
            relation = recording["relations"][0]
            if change == "isrc": data["isrc"] = "USUG11600659"
            elif change == "title": recording["title"] = "Epilogue (Live)"
            elif change == "artist": recording["artist-credit"] = [{"name": "Other Artist"}]
            elif change == "duration": recording["length"] = 455000
            elif change == "language": relation["work"]["languages"] = ["eng"]
            elif change == "unknown": relation["work"]["languages"] = []
            else: recording["relations"].append({**copy.deepcopy(relation), "work": {"id": "sung", "languages": ["eng"]}})
            with self.subTest(change=change): self.assertIsNone(self.confirm(isrc=data))

    def test_marker_is_exact_and_cannot_hide_a_sung_sentence(self):
        self.assertTrue(is_instrumental_marker("纯音乐，请欣赏"))
        self.assertTrue(is_instrumental_marker("纯音乐 / 无词配乐（无需逐词歌词）"))
        self.assertFalse(is_instrumental_marker("喜欢纯音乐，也喜欢你"))
        self.assertFalse(is_instrumental_marker("Instrumental love is all I need"))

    def gateway(self, root):
        gateway = object.__new__(LegacyGateway)
        gateway.state_dir = root / "state"
        audit = []
        def write(media, text, suffix):
            target = media.with_suffix(suffix); target.write_text(text)
            return str(target), ""
        gateway.service = SimpleNamespace(reference_documents=lambda _: [], existing_word_sidecar=lambda _, **__: None,
            write_text_sidecar=write, audit=SimpleNamespace(record=audit.append))
        gateway.legacy = SimpleNamespace(Result=lambda **kwargs: SimpleNamespace(**kwargs))
        return gateway

    def publish(self, gateway, track, fingerprint):
        with patch.dict("sys.modules", {"mutagen": SimpleNamespace(File=lambda _: SimpleNamespace(tags={}))}):
            return gateway.instrumental_result(track, fingerprint, ["Justin Hurwitz"],
                lrclib_fetch_function=lambda **_: RECORD, musicbrainz_fetch_function=lambda _: ISRC)

    def test_publication_marks_player_without_changing_audio_or_adding_word_cues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); media = root / "Epilogue.m4a"; media.write_bytes(b"immutable audio")
            track = SimpleNamespace(**vars(TRACK), path=media)
            before = media_fingerprint(media); gateway = self.gateway(root)
            result = self.publish(gateway, track, before)
            self.assertEqual(result["lyric_status"], "not_required")
            self.assertEqual(media_fingerprint(media), before)
            self.assertEqual(result["metrics"]["timed_words"], 0)
            self.assertTrue(is_instrumental_marker(media.with_suffix(".lrc").read_text().split(']', 1)[1]))
            evidence = json.loads(Path(result["metrics"]["source_evidence_path"]).read_text())
            self.assertEqual(evidence["source_fingerprint"], before)
            self.assertEqual(evidence["decision"]["isrc"], TRACK.isrc)

    def test_existing_plain_or_embedded_lyrics_veto_no_lyrics_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); media = root / "Epilogue.m4a"; media.write_bytes(b"immutable audio")
            track = SimpleNamespace(**vars(TRACK), path=media); gateway = self.gateway(root)
            media.with_suffix(".txt").write_text("This is a sung lyric")
            self.assertIsNone(self.publish(gateway, track, media_fingerprint(media)))
            self.assertFalse(media.with_suffix(".lrc").exists())
            media.with_suffix(".txt").unlink()
            with patch.dict("sys.modules", {"mutagen": SimpleNamespace(File=lambda _: SimpleNamespace(tags={"©lyr": ["This is a sung lyric"]}))}):
                self.assertIsNone(gateway.instrumental_result(track, media_fingerprint(media), ["Justin Hurwitz"],
                    lrclib_fetch_function=lambda **_: RECORD, musicbrainz_fetch_function=lambda _: ISRC))

    def test_normal_source_worker_uses_scan_stage_for_no_lyrics_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); media=root/'song.m4a'; media.write_bytes(b'audio')
            store=QueueStore(root/'queue.sqlite3'); queued=store.enqueue(media).job
            worker=object.__new__(SourceWorker); worker.store=store
            worker.engine=SimpleNamespace(resolve=lambda **_: Resolution(
                not_required={"lyric_status":"not_required","metrics":{"timed_words":0}},
                reason="confirmed_instrumental"))
            worker.process_source(store.claim('source', owner='test'))
            saved=store.get(queued.id)
            self.assertEqual(saved.status,'scan_pending')
            self.assertEqual(saved.result['lyric_status'],'not_required')


if __name__ == '__main__':
    unittest.main()
