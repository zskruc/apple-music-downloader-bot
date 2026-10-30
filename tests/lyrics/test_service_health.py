from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import zlib
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from word_lyrics_automation.service_health import (ServiceHealthMonitor, bounded_probe,
    amll_canary, evaluate_health, kugou_canary, navidrome_canary, netease_canary, queue_snapshot)


class Clock:
    def __init__(self, value=10000.0):
        self.value = value
    def __call__(self):
        return self.value


def probes(now):
    return {name: {"ok": True, "checked_at": now} for name in ("source", "player")}


def queue(jobs=()):
    return {"quick_check": "ok", "jobs": list(jobs), "counts": {}, "active": len(jobs)}


class HealthAssessmentTests(unittest.TestCase):
    def test_heartbeat_is_not_proof_of_working_source(self):
        now = 10000;records = probes(now)
        records["source"].update(ok=False, code="transport_failure")
        report = evaluate_health(worker={"updated_at": now}, queue=queue(), probes=records, now=now)
        self.assertEqual(report["status"], "degraded")
        self.assertFalse(report["business_ok"])
        self.assertTrue(report["liveness_ok"])
        self.assertFalse(report["restart_recommended"])
        self.assertEqual(report["reasons"][0]["code"], "source_probe_failed")

    def test_corrupt_media_is_attention_and_not_a_restart_loop(self):
        now = 10000
        job = {"id": 117, "status": "needs_attention", "created_at": 1,
            "fault": {"category": "media_integrity", "code": "flac_sample_count_mismatch", "retryable": False}}
        report = evaluate_health(worker={"updated_at": now}, queue=queue([job]), probes=probes(now), now=now)
        self.assertEqual(report["status"], "needs_attention")
        self.assertEqual(report["needs_attention"][0]["code"], "flac_sample_count_mismatch")
        self.assertTrue(report["liveness_ok"])
        self.assertFalse(report["restart_recommended"])

    def test_dead_worker_requires_action_even_with_cached_successful_canaries(self):
        report = evaluate_health(worker={"updated_at": 100}, queue=queue(), probes=probes(10000), now=10000)
        self.assertEqual(report["status"], "unhealthy")
        self.assertTrue(report["restart_recommended"])
        self.assertFalse(report["liveness_ok"])

    def test_corrupt_queue_does_not_recommend_repeated_restart(self):
        report = evaluate_health(worker={"updated_at": 10000}, queue={"quick_check": "bad"}, probes=probes(10000), now=10000)
        self.assertEqual(report["status"], "unhealthy")
        self.assertFalse(report["restart_recommended"])
        self.assertEqual(report["reasons"][0]["action"], "inspect_storage")

    def test_fresh_lease_heartbeats_do_not_hide_stalled_stage(self):
        job = {"id": 116, "status": "leased_alignment", "created_at": 100,
            "updated_at": 10000, "lease_expires_at": 20000, "stage_started_at": 100,
            "progress": {"stage": "recognizing", "unit": "whole-song", "completed_units": []}}
        report = evaluate_health(worker={"updated_at": 10000}, queue=queue([job]), probes=probes(10000), now=10000)
        self.assertIn("job_progress_stalled", [r["code"] for r in report["reasons"]])
        self.assertFalse(report["restart_recommended"])
        job["progress"]["completed_units"] = ["whole-song"]
        next_report = evaluate_health(worker={"updated_at": 10001}, queue=queue([job]), probes=probes(10001), now=10001,
            previous_observations=report["job_observations"])
        self.assertEqual(next_report["status"], "ok")

    def test_repeated_retry_claims_do_not_erase_original_outage(self):
        job = {"id": 1, "status": "retry_source", "created_at": 100, "updated_at": 10000,
            "stage_started_at": 10000, "fault": {"retryable": True, "first_failure_at": 100,
                "category": "transport", "code": "tls_eof"}}
        report = evaluate_health(worker={"updated_at": 10000}, queue=queue([job]), probes=probes(10000), now=10000)
        self.assertIn("dependency_retries_not_resolving", [r["code"] for r in report["reasons"]])

    def test_expired_lease_is_visible_without_telling_operator_to_restart_inputs(self):
        job = {"id": 3, "status": "leased_scan", "created_at": 9000,
            "stage_started_at": 9900, "lease_expires_at": 9000}
        report = evaluate_health(worker={"updated_at": 10000}, queue=queue([job]), probes=probes(10000), now=10000)
        self.assertEqual(report["status"], "unhealthy")
        self.assertIn("lease_not_reclaimed", [r["code"] for r in report["reasons"]])
        self.assertFalse(report["restart_recommended"])

    def test_old_probe_remains_stale_when_heartbeat_is_current(self):
        report = evaluate_health(worker={"updated_at": 10000}, queue=queue(), probes=probes(100), now=10000)
        self.assertEqual(report["status"], "degraded")
        self.assertEqual({r["code"] for r in report["reasons"]}, {"source_probe_stale", "player_probe_stale"})


def initialize_queue(path):
    with closing(sqlite3.connect(path)) as db:
        db.executescript("CREATE TABLE jobs(id integer,status text,lease_expires_at real,created_at real,updated_at real,payload_json text,last_error text);"
            "CREATE TABLE events(id integer,job_id integer,event text,created_at real);")


class PersistentMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);initialize_queue(self.root / "queue.sqlite3")
        self.clock = Clock();self.calls = []
    def good(self, name):
        def probe():
            self.calls.append(name);return {"ok": True, "code": "verified"}
        return probe
    def monitor(self, **kwargs):
        return ServiceHealthMonitor(self.root, clock=self.clock, source_probe=self.good("source"),
            player_probe=self.good("player"), probe_interval=300, **kwargs)
    def worker(self):
        return {"updated_at": self.clock()}

    def test_cache_and_schedule_survive_worker_restart(self):
        self.assertEqual(self.monitor().update(self.worker())["status"], "ok")
        self.assertEqual(self.calls, ["source", "player"])
        self.clock.value += 10
        self.assertEqual(self.monitor().update(self.worker())["status"], "ok")
        self.assertEqual(self.calls, ["source", "player"])
        self.clock.value += 300
        self.monitor().update(self.worker())
        self.assertEqual(self.calls, ["source", "player", "source", "player"])

    def test_cached_check_does_not_refresh_probe_freshness(self):
        monitor = self.monitor(probe_max_age=600)
        monitor.update(self.worker());self.clock.value += 1000
        result = monitor.update(self.worker(), run_probes=False)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["probe_results"]["source"]["checked_at"], 10000)
        self.assertEqual(len(self.calls), 2)

    def test_outage_backoff_persists_and_recovers_without_restarting_worker(self):
        failures = [True]
        def source():
            self.calls.append("source")
            return {"ok": not failures[0], "code": "transport_failure" if failures[0] else "verified"}
        def make():
            return ServiceHealthMonitor(self.root, clock=self.clock, source_probe=source,
                player_probe=self.good("player"), probe_interval=300)
        first = make().update(self.worker())
        self.assertEqual(first["status"], "degraded")
        self.assertEqual(first["probe_results"]["source"]["next_probe_at"], 10600)
        self.clock.value += 301;make().update(self.worker())
        self.assertEqual(self.calls.count("source"), 1)
        self.clock.value = 10600;failures[0] = False
        recovered = make().update(self.worker())
        self.assertEqual(recovered["status"], "ok")
        self.assertEqual(recovered["probe_results"]["source"]["consecutive_failures"], 0)
        self.assertEqual(recovered["probe_results"]["source"]["recovery_generation"], 1)
        self.assertEqual(recovered["probe_results"]["source"]["recovered_at"], 10600)
        self.assertEqual(recovered["probe_results"]["source"]["last_failure_at"], 10000)
        self.assertEqual(recovered["probe_results"]["source"]["capability"], "source")
        self.clock.value += 1
        self.assertEqual(make().update(self.worker())["probe_results"]["source"]["recovery_generation"], 1)

    def test_changed_canary_context_is_not_masked_by_old_cache(self):
        self.monitor(source_spec={"lyric_id": "first"}).update(self.worker())
        self.monitor(source_spec={"lyric_id": "second"}).update(self.worker())
        self.assertEqual(self.calls.count("source"), 2)
        self.assertEqual(self.calls.count("player"), 1)
        record = self.monitor(source_spec={"lyric_id": "second"}).update(self.worker())["probe_results"]["source"]
        self.assertEqual(record["recovery_generation"], 0)
        self.assertIsNone(record["recovered_at"])

    def test_queue_read_does_not_change_database_or_leak_exception_credentials(self):
        secret = "https://user:secret@source.invalid?q=private"
        with closing(sqlite3.connect(self.root / "queue.sqlite3")) as db:
            db.execute("INSERT INTO jobs VALUES(1,'waiting_source',0,1,1,?,?)", (json.dumps({"source_state": {"code": "tls_eof", "category": "transport", "retryable": True}}), secret))
            db.execute("INSERT INTO events VALUES(1,1,'claimed',1)")
            db.commit()
        before = (self.root / "queue.sqlite3").read_bytes()
        snapshot = queue_snapshot(self.root / "queue.sqlite3", now=self.clock())
        self.assertEqual(snapshot["quick_check"], "ok")
        self.assertNotIn("secret", json.dumps(snapshot))
        self.assertNotIn("private", json.dumps(snapshot))
        self.assertEqual(before, (self.root / "queue.sqlite3").read_bytes())

    def test_corrupt_probe_cache_is_rebuilt(self):
        (self.root / "service-probe-state.json").write_text("{broken")
        result = self.monitor().update(self.worker())
        self.assertEqual(result["status"], "ok")
        self.assertEqual((self.root / "service-probe-state.json").stat().st_mode & 0o077, 0)

    def test_additional_provider_has_its_own_persisted_recovery_evidence(self):
        failed = [True]
        def amll():
            return {"ok": not failed[0], "code": "transport_failure" if failed[0] else "valid_source_discovery_response"}
        def make():
            return ServiceHealthMonitor(self.root, clock=self.clock, probe_interval=30,
                source_probe=self.good("source"), player_probe=self.good("player"),
                additional_source_specs={"amll": {"title": "query"}}, additional_source_probes={"amll": amll})
        first = make().update(self.worker())
        self.assertEqual(first["status"], "degraded")
        self.assertEqual(first["probe_results"]["source_amll"]["provider"], "amll")
        self.clock.value += 61;failed[0] = False
        recovered = make().update(self.worker())
        self.assertEqual(recovered["status"], "ok")
        self.assertEqual(recovered["probe_results"]["source_amll"]["recovery_generation"], 1)
        self.assertEqual(recovered["probe_results"]["source_amll"]["capability"], "source")
        self.assertEqual(recovered["probe_results"]["source"]["recovery_generation"], 0)


class Reply(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.close()


class Opener:
    def __init__(self, replies):
        self.replies = list(replies);self.requests = []
    def open(self, request, timeout):
        self.requests.append((request.full_url, timeout))
        response = self.replies.pop(0)
        if isinstance(response, Exception):
            raise response
        return Reply(response if isinstance(response, bytes) else json.dumps(response).encode())


class RealProbeProtocolTests(unittest.TestCase):
    def source(self):
        return {"title": "Test", "artist": "Artist", "duration_ms": 10000, "lyric_id": "99", "minimum_cues": 4}
    def candidate(self):
        return {"id": "99", "accesskey": "private-access-key", "product_from": "官方推荐歌词", "song": "Test", "singer": "Artist", "duration": 10000}
    def krc(self):
        raw = "\n".join(f"[{1000 + i * 1000},1000]<0,1000,0>甲" for i in range(4)).encode()
        from word_lyrics_automation.providers.kugou import _KRC_KEY
        compressed = zlib.compress(raw)
        encoded = b"krc1" + bytes(x ^ _KRC_KEY[i % len(_KRC_KEY)] for i, x in enumerate(compressed))
        return {"content": base64.b64encode(encoded).decode()}

    def test_source_probe_fetches_and_decodes_not_just_http_200(self):
        opener = Opener([{"candidates": [self.candidate()]}, self.krc()])
        result = kugou_canary(self.source(), opener=opener)
        self.assertTrue(result["ok"])
        self.assertEqual(result["lines"], 4)
        self.assertEqual(len(opener.requests), 2)
        self.assertNotIn("private-access-key", json.dumps(result))
        self.assertNotIn("甲", json.dumps(result, ensure_ascii=False))

    def test_source_bad_json_http_200_is_failure(self):
        result = kugou_canary(self.source(), opener=Opener([b"<html>rate limit</html>"]))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_provider_response")

    def test_source_nonofficial_identity_is_rejected_before_download(self):
        candidate = self.candidate();candidate["product_from"] = "ugc"
        opener = Opener([{"candidates": [candidate]}])
        result = kugou_canary(self.source(), opener=opener)
        self.assertEqual(result["code"], "canary_identity_changed")
        self.assertEqual(len(opener.requests), 1)

    def test_source_missing_canary_is_not_success(self):
        result = kugou_canary(self.source(), opener=Opener([{"candidates": []}]))
        self.assertEqual(result["code"], "canary_recording_unavailable")

    def test_amll_empty_search_is_available_without_claiming_a_recording_match(self):
        result = amll_canary({"title": "query", "artist": "artist"}, opener=Opener([{"data": {"items": []}}]))
        self.assertTrue(result["ok"])
        self.assertEqual(result["result_count"], 0)
        self.assertEqual(result["scope"], "discovery_api_availability_not_recording_match")
        invalid = amll_canary({"title": "query"}, opener=Opener([{"success": True}]))
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["code"], "invalid_provider_response")

    def test_netease_probe_requires_actual_word_lyric_semantics(self):
        spec = {"song_id": "99", "minimum_cues": 4}
        yrc = "\n".join(f"[{1000 + i * 1000},1000]({1000 + i * 1000},1000,0)甲" for i in range(4))
        result = netease_canary(spec, opener=Opener([{"code": 200, "lrc": {"lyric": "[00:01]甲"}, "yrc": {"lyric": yrc}}]))
        self.assertTrue(result["ok"]);self.assertEqual(result["cues"], 4)
        missing = netease_canary(spec, opener=Opener([{"code": 200, "lrc": {"lyric": "[00:01]甲"}}]))
        self.assertEqual(missing["code"], "canary_lyric_unavailable")

    def test_authentication_error_is_sanitized_and_not_network_retry(self):
        exc = urllib.error.HTTPError("https://secret@host/?accesskey=private", 403, "secret", {}, None)
        result = kugou_canary(self.source(), opener=Opener([exc]))
        self.assertEqual(result["code"], "authentication_or_permission")
        self.assertFalse(result["retryable"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("private", json.dumps(result))

    def test_wall_clock_deadline_kills_a_stuck_probe_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "word_lyrics_automation";package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "service_health.py").write_text("import time; time.sleep(30)")
            started = time.monotonic()
            import subprocess
            real_run = subprocess.run
            # /app contains the installed package in deployment images; a
            # fixture PYTHONPATH alone cannot override the working directory.
            def child(arguments, **kwargs):
                return real_run(arguments, cwd=directory, **kwargs)
            with patch.dict(os.environ, {"PYTHONPATH": directory}), patch("word_lyrics_automation.service_health.subprocess.run", side_effect=child):
                result = bounded_probe("source", {}, deadline_seconds=1)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(result["code"], "probe_deadline_exceeded")

    def test_player_probe_checks_all_word_clocks_and_utf8_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            sidecar = Path(directory) / "sample.ttml"
            sidecar.write_text('<tt xmlns="http://www.w3.org/ns/ttml"><body><div><p begin="00:01.000" end="00:02.000"><span begin="00:01.000" end="00:01.400">甲</span><span begin="00:01.400" end="00:02.000">乙</span></p></div></body></tt>')
            cues = [{"value": "甲", "start": 1000, "end": 1400, "byteStart": 0, "byteEnd": 2},
                {"value": "乙", "start": 1400, "end": 2000, "byteStart": 3, "byteEnd": 5}]
            response = {"subsonic-response": {"status": "ok", "lyricsList": {"structuredLyrics": [{"line": [{"start": 1000, "value": "甲乙"}], "cueLine": [
                {"index": 0, "value": "甲乙", "start": 1000, "end": 2000, "cue": cues}]}]}}}
            spec = {"song_id": "one", "sidecar_path": str(sidecar), "candidate_sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest()}
            with patch.dict(os.environ, {"NAVIDROME_USER": "test", "NAVIDROME_PASSWORD": "secret"}):
                result = navidrome_canary(spec, opener=Opener([response]))
                self.assertTrue(result["ok"])
                self.assertEqual(result["cues"], 2)
                cues[0]["end"] += 1
                mismatch = navidrome_canary(spec, opener=Opener([response]))
            self.assertEqual(mismatch["code"], "player_word_text_clock_or_byte_range_not_current")
            self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
