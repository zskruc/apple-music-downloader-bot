"""Offline failure injection through the real durable worker lifecycle.

Only external sources and the Navidrome server are fixtures. Source parsing,
publication validation, media decoding, queue/retry policy and player read-back
are the production implementations. No verifier is replaced with True.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
import wave
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from word_lyrics_automation.fingerprint import media_fingerprint
from word_lyrics_automation.providers.amll import AMLLClient
from word_lyrics_automation.queue import QueueStore
from word_lyrics_automation.resolver import LegacyGateway, SourceResolutionEngine
from word_lyrics_automation.service_health import (ServiceHealthMonitor, evaluate_health,
    navidrome_canary, queue_snapshot)
from word_lyrics_automation.source_worker import SourceWorker


TTML = ('<tt xmlns="http://www.w3.org/ns/ttml" xmlns:i="http://music.apple.com/lyric-ttml-internal" i:timing="Word">'
    '<body><div><p begin="00:01.000" end="00:03.000"><span begin="00:01.000" end="00:02.000">Hello </span>'
    '<span begin="00:02.000" end="00:03.000">world</span></p><p begin="00:04.000" end="00:06.000">'
    '<span begin="00:04.000" end="00:05.000">Good </span><span begin="00:05.000" end="00:06.000">bye</span>'
    '</p></div></body></tt>')


class Clock:
    def __init__(self):
        self.value = 10000.0
    def __call__(self):
        return self.value


class IsolatedGateway(LegacyGateway):
    # The only upstream under test is the local AMLL fixture. Disable unrelated
    # online catalog lookups, while retaining every publication/media gate.
    def localized_artist_identity(self, *_):
        return {"names": [], "evidence": []}
    def instrumental_result(self, *_args, **_kwargs):
        return None


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "full decoder tools required")
class UnattendedLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name);self.state = self.root / "state"
        self.audio = self.root / "song.wav"
        with wave.open(str(self.audio), "wb") as f:
            f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b"\0\0" * 16000 * 8)
        self.original = self.audio.read_bytes();self.clock = Clock()
        self.metadata = {"title": "Song", "artist": "Artist", "album": "Album", "duration": 8.0,
            "song_id": "123456789", "notification": {"source": "telegram", "chat_id": "fixture", "task_id": "42", "message_id": "fixture"}}
        self.upstream_failed = True;self.upstream_bad_json = False;self.player_stale = False
        self.source_requests = 0;self.scan_requests = 0;self.player_ids = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def emit(self, payload, status=200):
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status);self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path);query = urllib.parse.parse_qs(parsed.query)
                if parsed.path.startswith("/v1/lyrics/"):
                    test.source_requests += 1
                    if test.upstream_failed:
                        self.emit({"error": "temporary fixture outage"}, 503);return
                    if test.upstream_bad_json:
                        self.emit(b"<html>provider maintenance page</html>");return
                    item = {"id": "fixture", "filename": "fixture.ttml", "musicNames": ["Song"],
                        "artistNames": ["Artist"], "albumNames": ["Album"], "appleMusicIds": ["123456789"], "lyrics": TTML}
                    self.emit({"data": {"items": [item]}} if parsed.path.endswith("/search") else {"data": item});return
                if parsed.path.startswith("/rest/"):
                    salt = query.get("s", [""])[0]
                    valid = query.get("u") == ["fixture-user"] and query.get("t") == [hashlib.md5(("fixture-password" + salt).encode(), usedforsecurity=False).hexdigest()]
                    if not valid:
                        self.emit({"subsonic-response": {"status": "failed", "error": {"code": 40}}});return
                    result = {"status": "ok"}
                    if parsed.path.endswith("startScan.view"):
                        test.scan_requests += 1
                    elif parsed.path.endswith("search3.view"):
                        result["searchResult3"] = {"song": [
                            {"id": "older-mp3", "title": "Song", "artist": "Artist", "album": "Album", "size": 42, "suffix": "mp3"},
                            {"id": "target", "title": "Song", "artist": "Artist", "album": "Album", "size": len(test.original), "suffix": "wav"}]}
                    elif parsed.path.endswith("getLyricsBySongId.view"):
                        test.player_ids.append(query.get("id", [""])[0])
                        result["lyricsList"] = {"structuredLyrics": [{"kind": "main", "line": [
                            {"start": 1000, "value": "Hello world"}, {"start": 4000, "value": "Good bye"}], "cueLine": [
                            {"index": 0, "start": 1000, "end": 3000, "value": "Hello world", "cue": [
                                {"start": 1000, "end": 2000 + int(test.player_stale), "value": "Hello ", "byteStart": 0, "byteEnd": 5},
                                {"start": 2000, "end": 3000, "value": "world", "byteStart": 6, "byteEnd": 10}]},
                            {"index": 1, "start": 4000, "end": 6000, "value": "Good bye", "cue": [
                                {"start": 4000, "end": 5000, "value": "Good ", "byteStart": 0, "byteEnd": 4},
                                {"start": 5000, "end": 6000, "value": "bye", "byteStart": 5, "byteEnd": 7}]}]}]}
                    else:
                        self.emit({"subsonic-response": {"status": "failed", "error": {"code": 70}}});return
                    self.emit({"subsonic-response": result});return
                self.emit({}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.environment = patch.dict(os.environ, {"AMDL_WORD_LYRICS_STATE_DIR": str(self.state),
            "NAVIDROME_URL": self.base, "NAVIDROME_USER": "fixture-user", "NAVIDROME_PASSWORD": "fixture-password",
            "HTTP_PROXY":"http://127.0.0.1:1", "HTTPS_PROXY":"http://127.0.0.1:1", "NO_PROXY":"", "no_proxy":"",
            "WORD_LYRICS_MEDIA_INTEGRITY_ENABLED": "true", "WORD_LYRICS_PLAYER_VERIFY_ENABLED": "true"})
        self.environment.start();self.addCleanup(self.environment.stop)
        # Imported scan adapter uses module configuration; retain its real HTTP
        # implementation and replace only the external server configuration.
        import youtube_ingest
        scan_config = patch.multiple(youtube_ingest, NAVIDROME_URL=self.base,
            NAVIDROME_USER="fixture-user", NAVIDROME_PASSWORD="fixture-password")
        scan_config.start();self.addCleanup(scan_config.stop)
        self.worker = self.worker_after_restart()

    def worker_after_restart(self):
        worker = SourceWorker()
        worker.store = QueueStore(self.state / "automation/queue.sqlite3", clock=self.clock)
        worker.gateway = IsolatedGateway(self.state)
        client = AMLLClient(base_url=self.base, timeout=5, attempts=1)
        worker.engine = SourceResolutionEngine(worker.gateway, amll_client=client,
            netease_search_function=lambda *_args, **_kwargs: [],
            kugou_search_function=lambda **_kwargs: [])
        return worker

    def enqueue(self, audio=None):
        return self.worker.store.enqueue(audio or self.audio, metadata=self.metadata).job

    def source_once(self):
        job = self.worker.store.claim("source", owner="fixture-source", lease_seconds=120)
        self.assertIsNotNone(job);self.worker.process_source(job)
        return self.worker.store.get(job.id)

    def scan_once(self):
        job = self.worker.store.claim("scan", owner="fixture-scan", lease_seconds=120)
        self.assertIsNotNone(job);self.worker.process_scan(job)
        return self.worker.store.get(job.id)

    def test_transport_outage_restart_recovery_publication_and_real_player_readback(self):
        job = self.enqueue();self.assertEqual(self.enqueue().id, job.id)
        first = self.source_once()
        self.assertEqual(first.status, "retry_source")
        self.assertEqual(first.payload["source_state"]["category"], "transport")
        self.assertEqual(first.payload["source_state"]["code"], "http_server_error")
        self.assertGreater(first.next_attempt_at, self.clock())
        self.assertIsNone(self.worker.store.claim("source", owner="too-early"))
        self.assertFalse(self.audio.with_suffix(".ttml").exists())
        # A new worker process reads exactly the same durable job and history.
        self.worker = self.worker_after_restart();self.upstream_failed = False
        self.clock.value = first.next_attempt_at + 1
        published = self.source_once()
        self.assertEqual(published.id, job.id);self.assertEqual(published.status, "scan_pending")
        self.assertEqual(self.audio.with_suffix(".ttml").read_text(), TTML)
        digest = hashlib.sha256(self.audio.with_suffix(".ttml").read_bytes()).hexdigest()
        # A scan acknowledgement cannot conceal even a one-millisecond stale
        # word cue. The real HTTP verifier must refuse completion and retry.
        self.player_stale = True;stale = self.scan_once()
        self.assertEqual(stale.status, "retry_scan")
        self.assertEqual(self.scan_requests, 1)
        self.assertEqual(hashlib.sha256(self.audio.with_suffix(".ttml").read_bytes()).hexdigest(), digest)
        requests_before_restart = self.source_requests
        self.worker = self.worker_after_restart();self.player_stale = False
        self.clock.value = stale.next_attempt_at + 1
        completed = self.scan_once()
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.result["player_verification"]["cues"], 4)
        self.assertEqual(self.source_requests, requests_before_restart)
        self.assertEqual(self.player_ids, ["target", "target"])
        self.assertEqual(self.audio.read_bytes(), self.original)
        self.assertEqual(sum(event["event"] == "source_recovered" for event in self.worker.store.events(job.id)), 1)
        self.assertEqual(self.enqueue().id, job.id)
        with closing(self.worker.store.connect()) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM notification_subscriptions").fetchone()[0], 1)
        spec = {"song_id": "target", "sidecar_path": str(self.audio.with_suffix(".ttml")), "candidate_sha256": digest,
            "media_path": str(self.audio), "source_fingerprint": media_fingerprint(self.audio)}
        canary = navidrome_canary(spec)
        self.assertTrue(canary["ok"]);self.assertTrue(canary["media_binding_verified"])
        monitor = ServiceHealthMonitor(self.state / "automation", clock=self.clock,
            source_probe=lambda: {"ok": False, "code": "transport_failure"}, player_probe=lambda: canary, probe_interval=30)
        degraded = monitor.update({"updated_at": self.clock()})
        self.assertEqual(degraded["status"], "degraded");self.assertFalse(degraded["restart_recommended"])
        self.clock.value += 61
        monitor = ServiceHealthMonitor(self.state / "automation", clock=self.clock,
            source_probe=lambda: {"ok": True, "code": "source_recovered"}, player_probe=lambda: navidrome_canary(spec), probe_interval=30)
        healthy = monitor.update({"updated_at": self.clock()})
        self.assertEqual(healthy["status"], "ok")

    def test_sample_count_mismatch_holds_before_any_source_request_and_survives_restart(self):
        corrupt = self.root / "bad.flac"
        conversion = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-i", str(self.audio), str(corrupt)], capture_output=True, check=False)
        self.assertEqual(conversion.returncode, 0)
        data = bytearray(corrupt.read_bytes());self.assertEqual(data[:4], b"fLaC")
        packed = int.from_bytes(data[18:26], "big")
        packed = (packed & ~((1 << 36) - 1)) | ((packed & ((1 << 36) - 1)) + 16000)
        data[18:26] = packed.to_bytes(8, "big");corrupt.write_bytes(data)
        before = corrupt.read_bytes();job = self.enqueue(corrupt)
        held = self.source_once()
        self.assertEqual(held.status, "needs_attention")
        self.assertEqual(held.payload["source_state"]["category"], "media_integrity")
        self.assertEqual(held.payload["source_state"]["code"], "media_header_mismatch")
        self.assertEqual(self.source_requests, 0);self.assertFalse(corrupt.with_suffix(".ttml").exists())
        self.worker = self.worker_after_restart();self.clock.value += 8 * 3600
        self.assertIsNone(self.worker.store.claim("source", owner="restart"))
        self.assertEqual(self.enqueue(corrupt).id, job.id)
        self.assertEqual(self.worker.store.reconcile_source_holds(), [])
        self.assertEqual(corrupt.read_bytes(), before)
        now = self.clock();ok_probes = {name: {"ok": True, "checked_at": now} for name in ("source", "player")}
        report = evaluate_health(worker={"updated_at": now}, queue=queue_snapshot(self.worker.store.database_path, now=now), probes=ok_probes, now=now)
        self.assertEqual(report["status"], "needs_attention");self.assertFalse(report["restart_recommended"])

    def test_expired_source_lease_is_reclaimed_after_worker_loss_without_duplicate_job(self):
        job = self.enqueue();abandoned = self.worker.store.claim("source", owner="lost-worker", lease_seconds=30)
        self.clock.value = abandoned.lease_expires_at + 1
        self.worker = self.worker_after_restart();self.assertEqual(self.worker.store.reclaim_expired(), 1)
        retry = self.worker.store.get(job.id);self.clock.value = retry.next_attempt_at + 1
        self.upstream_failed = False
        published = self.source_once();self.assertEqual(published.id, job.id)
        self.assertEqual(self.scan_once().status, "completed")
        self.assertEqual(self.enqueue().id, job.id)
        self.assertEqual(self.audio.read_bytes(), self.original)

    def test_http_200_maintenance_document_is_a_parse_hold_not_a_network_loop(self):
        self.upstream_failed = False;self.upstream_bad_json = True
        job = self.enqueue();held = self.source_once()
        self.assertEqual(held.status, "needs_attention")
        self.assertEqual(held.payload["source_state"]["category"], "parse")
        self.assertEqual(held.payload["source_state"]["code"], "invalid_provider_document")
        self.assertFalse(self.audio.with_suffix(".ttml").exists())
        count = self.source_requests
        self.worker = self.worker_after_restart();self.clock.value += 8 * 3600
        self.assertIsNone(self.worker.store.claim("source", owner="restart"))
        self.assertEqual(self.enqueue().id, job.id)
        self.assertEqual(self.source_requests, count)


if __name__ == "__main__":
    unittest.main()
