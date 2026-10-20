import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bot"))
from lx_fallback import AppleTrack
from sq_music import (SqMusicClient, SqMusicError, SqMusicStopped,
                      SqSearchItem, item_from_record, select_candidate)


def record(**values):
    return dict({"id": "123", "name": "Song", "artistName": ["Singer"], "albumName": "Album",
                 "duration": "180000", "plugName": "netease",
                 "brTypes": ["NETEASE_MP3_320", "NETEASE_FLAC_2000", "NETEASE_FLAC_3000"]}, **values)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = SqMusicClient("http://sqmusic_main:8099", "fixture-user", "fixture-password",
                                    staging_root=self.root, sleep=lambda _: None)
        self.item = item_from_record(record())

    def completed(self, **values):
        return dict({"id": 42, "downloadMusicId": "123", "downloadPlugName": "netease",
                     "downloadBrType": "netease_flac_3000", "downloadStatus": "success",
                     "downloadFile": "/music/42/Singer/Album/Song.flac"}, **values)

    def output(self, name="42/Singer/Album/Song.flac", content=None):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if content is not None else b"fLaC" + b"\0" * 100)
        return path

    def test_enum_quality_and_ms_duration(self):
        self.assertEqual(self.item.quality, "netease_flac_3000")
        self.assertEqual(self.item.duration_seconds, 180)

    def test_mp3_and_ape_do_not_qualify(self):
        self.assertIsNone(item_from_record(record(brTypes=["NETEASE_MP3_320", "NETEASE_APE_2000"])))
        self.assertIsNone(item_from_record(record(brTypes=["QQVIP_Flac_3000"])))

    def test_match_rejects_versions_and_unknown_evidence(self):
        track = AppleTrack("a", "Song", "Singer", "Album", 180)
        self.assertIsNotNone(select_candidate(track, [self.item]))
        self.assertIsNone(select_candidate(track, [item_from_record(record(albumName="Album (Remastered)"))]))
        self.assertIsNone(select_candidate(track, [item_from_record(record(duration="184000"))]))
        self.assertIsNone(select_candidate(AppleTrack("a", "Song", "Singer"), [self.item]))

    def test_real_file_task_path(self):
        path = self.output()
        self.assertEqual(self.client._task_file(self.completed(), self.item, 42), path.resolve())

    def test_display_name_and_legacy_path_fail_closed(self):
        self.output()
        for raw in ["Song - Singer", "/music/Singer/Album/Song.flac", "/music/43/Song.flac",
                    "/music/42/../43/Song.flac", "/other/42/Song.flac", "C:\\music\\42\\Song.flac"]:
            with self.subTest(path=raw), self.assertRaises(SqMusicError):
                self.client._task_file(self.completed(downloadFile=raw), self.item, 42)

    def test_task_identity_and_quality(self):
        self.output()
        for values in [{"id": 43}, {"downloadMusicId": "124"}, {"downloadPlugName": "kw"},
                       {"downloadBrType": "netease_mp3_320"}]:
            with self.subTest(values=values), self.assertRaises(SqMusicError):
                self.client._task_file(self.completed(**values), self.item, 42)

    def test_symlink_and_fake_flac_rejected(self):
        target = self.output("outside.flac")
        path = self.root / "42/Singer/Album/Song.flac"
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
        with self.assertRaises(SqMusicError):
            self.client._task_file(self.completed(), self.item, 42)
        path.unlink()
        path.write_bytes(b"ID3" + b"\0" * 100)
        with self.assertRaises(SqMusicError):
            self.client._task_file(self.completed(), self.item, 42)

    def test_waits_for_real_success_and_uses_server_enum(self):
        self.output()
        request = Mock(side_effect=[{"id": 42}, {"task": self.completed(downloadStatus="loading")},
                                    {"task": self.completed()}])
        self.client._request_json = request
        progress = []
        self.client.download_candidate(self.item, on_progress=progress.append)
        self.assertEqual(progress, ["loading", "success"])
        self.assertEqual(request.call_args_list[0].kwargs["payload"]["brType"], "NETEASE_FLAC_3000")
        self.assertEqual(request.call_args_list[0].kwargs["payload"]["downloadFormat"], "flac")

    def test_supplement_success_is_not_final_success(self):
        self.client._request_json = Mock(side_effect=[{"id": 42}, {"task": self.completed(downloadStatus="supplement_success")}])
        with self.assertRaises(SqMusicError):
            self.client.download_candidate(self.item)

    def test_pre_enqueue_cancel_has_no_side_effect(self):
        self.client._request_json = Mock()
        with self.assertRaises(SqMusicStopped):
            self.client.download_candidate(self.item, stop_requested=lambda: True)
        self.client._request_json.assert_not_called()

    def test_changed_metadata_does_not_enqueue_another_song(self):
        self.client._request_json = Mock()
        with self.assertRaises(SqMusicError):
            self.client.download_candidate(replace(self.item, song_id="different"))
        self.client._request_json.assert_not_called()

    def test_inflight_cancel_does_not_delete_files_or_other_jobs(self):
        self.output()
        self.client._request_json = Mock(return_value={"id": 42})
        stop = iter([False, True])
        with self.assertRaises(SqMusicStopped):
            self.client.download_candidate(self.item, stop_requested=lambda: next(stop))
        self.assertEqual(self.client._request_json.call_count, 1)
        self.assertTrue((self.root / "42/Singer/Album/Song.flac").exists())

    def test_timeout_does_not_guess_or_delete_output(self):
        clock = iter([0, 2])
        self.client.monotonic = lambda: next(clock)
        self.client._request_json = Mock(return_value={"id": 42})
        with self.assertRaisesRegex(SqMusicError, "timed out"):
            self.client.download_candidate(self.item, timeout_seconds=1)


class AuthTests(unittest.TestCase):
    def test_real_sq_login_response_token_value(self):
        client = SqMusicClient("http://sq:8099", "fixture-user", "fixture-password")
        client._raw_request = Mock(return_value={"code": 200, "data": {
            "tokenName": "sqmusic", "tokenValue": "actual-token", "isLogin": True,
        }})
        self.assertEqual(client._login(), "actual-token")

    def test_auth_refresh_and_no_credentials_in_error(self):
        client = SqMusicClient("http://sq:8099", "secret-user", "secret-password")
        client._raw_request = Mock(side_effect=[
            {"code": 200, "data": {"token": "token-one"}},
            {"code": 401},
            {"code": 200, "data": {"token": "token-two"}},
            {"code": 200, "data": {"records": []}},
        ])
        self.assertEqual(client._request_json("/api/music/searchSong"), {"records": []})
        self.assertEqual(client._token, "token-two")
        client._raw_request = Mock(return_value={"code": 500, "msg": "secret-user secret-password"})
        with self.assertRaises(SqMusicError) as error:
            client._request_json("/api/music/searchSong")
        self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
