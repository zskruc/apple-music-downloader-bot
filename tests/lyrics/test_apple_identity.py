from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from word_lyrics_automation.providers.apple_identity import AppleArtistIdentity


class AppleIdentityTests(unittest.TestCase):
    def test_regional_names_share_catalog_identity_and_are_cached(self) -> None:
        calls = []
        track = SimpleNamespace(song_id="123", artist="English Name", isrc="JP0000000001", duration=190.5)

        class Client:
            def request_json(self, url):
                calls.append(url)
                if "/songs/" in url:
                    return 200, {"data": [{
                        "id": "123", "attributes": {"artistName": "English Name", "durationInMillis": 190500, "isrc": "JP0000000001"},
                        "relationships": {"artists": {"data": [{"id": "456", "type": "artists"}]}},
                    }]}
                name = "日本語名" if "/jp/" in url else "English Name"
                return 200, {"data": [{"id": "456", "type": "artists", "attributes": {"name": name}}]}

        with tempfile.TemporaryDirectory() as directory:
            provider = AppleArtistIdentity(Path(directory), Client)
            result = provider.resolve(track)
            self.assertEqual(result["names"], ["日本語名", "English Name"])
            self.assertTrue(any(row.get("artist_id") == "456" for row in result["evidence"]))
            count = len(calls)
            self.assertEqual(provider.resolve(track), result)
            self.assertEqual(len(calls), count)

    def test_wrong_song_duration_isrc_or_artist_never_grants_an_alias(self) -> None:
        track = SimpleNamespace(song_id="123", artist="English Name", isrc="JP0000000001", duration=190.5)
        valid = {"artistName": "English Name", "durationInMillis": 190500, "isrc": "JP0000000001"}
        for wrong in ({"durationInMillis": 180000}, {"isrc": "different"}, {"artistName": "Someone Else"}):
            class Client:
                def request_json(self, url):
                    if "/artists/" in url:
                        raise AssertionError("unverified recording must not resolve artist aliases")
                    return 200, {"data": [{"id": "123", "attributes": {**valid, **wrong}, "relationships": {"artists": {"data": [{"id": "456", "type": "artists"}]}}}]}
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(AppleArtistIdentity(Path(directory), Client).resolve(track)["names"], [])

    def test_wrong_artist_id_and_temporary_failure_are_not_trusted(self) -> None:
        track = SimpleNamespace(song_id="123", artist="English Name", isrc="", duration=190.5)
        class Client:
            def request_json(self, url):
                if "/songs/" in url:
                    return 200, {"data": [{"id": "123", "attributes": {"artistName": "English Name", "durationInMillis": 190500}, "relationships": {"artists": {"data": [{"id": "456", "type": "artists"}]}}}]}
                return 200, {"data": [{"id": "999", "type": "artists", "attributes": {"name": "Wrong Artist"}}]}
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(AppleArtistIdentity(Path(directory), Client).resolve(track)["names"], [])
        class Unavailable:
            def request_json(self, url):
                return 503, {}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "503"):
                AppleArtistIdentity(Path(directory), Unavailable).resolve(track)
            self.assertEqual(list(Path(directory).glob("*.json")), [])
