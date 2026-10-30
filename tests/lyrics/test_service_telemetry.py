import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from word_lyrics_automation.telemetry import update_telemetry


class TelemetryTests(unittest.TestCase):
    def test_rollout_window_excludes_historical_success_and_empty_rate_is_null(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "queue.sqlite3")) as db:
                db.executescript("CREATE TABLE jobs(id integer,status text,created_at real);CREATE TABLE events(job_id integer,event text,created_at real,detail_json text default '{}');")
                db.execute("INSERT INTO jobs VALUES(0,'dead_letter',5)")
                db.execute("INSERT INTO jobs VALUES(1,'completed',10)");db.execute("INSERT INTO events(job_id,event,created_at) VALUES(1,'completed',20)");db.commit()
            health = {"status": "ok", "reasons": [], "needs_attention": []}
            empty = update_telemetry(root, health, rollout_started_at=100, now=1000)
            self.assertEqual(empty["total_new_jobs"], 0);self.assertIsNone(empty["completion_rate"])
            self.assertIsNone(empty["auto_completion_rate"])
            self.assertEqual(empty["historical_dead_letter_ids"], [0])
            with closing(sqlite3.connect(root / "queue.sqlite3")) as db:
                db.executemany("INSERT INTO jobs VALUES(?,?,?)", [(2, "completed", 200), (3, "needs_attention", 210), (4, "retry_source", 220), (5, "superseded", 230)])
                db.executemany("INSERT INTO events(job_id,event,created_at) VALUES(?,?,?)", [(2, "completed", 250), (2, "source_recovered", 240), (4, "dependency_recovery_resumed", 245)]);db.commit()
            result = update_telemetry(root, health, rollout_started_at=100, now=1100)
            self.assertEqual(result["total_new_jobs"], 3);self.assertEqual(result["completed"], 1)
            self.assertAlmostEqual(result["completion_rate"], 1 / 3)
            self.assertEqual(result["completion_latency_seconds"]["mean"], 50)
            self.assertEqual(result["source_recoveries"], 1)
            self.assertEqual(result["verified_dependency_resumptions"], 1)
            self.assertEqual(result["auto_completed_without_manual_event"], 1)
            self.assertEqual(result["manual_event_jobs"], 0)
            with patch("word_lyrics_automation.telemetry._measure", side_effect=AssertionError("must use the 60-second cache")):
                cached = update_telemetry(root, {**health, "status": "degraded"}, rollout_started_at=100, now=1101)
            self.assertEqual(cached["total_new_jobs"], 3)
            content = json.loads((root / "service-telemetry.json").read_text())
            self.assertEqual(len(content["state_changes"]), 2)
            self.assertEqual((root / "service-telemetry.json").stat().st_mode & 0o077, 0)
            with closing(sqlite3.connect(root / "queue.sqlite3")) as db:
                db.execute("INSERT INTO events VALUES(2,'source_context_changed',249,?)", (json.dumps({"manual_review": True}),))
                db.execute("INSERT INTO jobs VALUES(6,'completed',300)")
                db.execute("INSERT INTO events(job_id,event,created_at) VALUES(6,'completed',350)");db.commit()
            final = update_telemetry(root, health, rollout_started_at=100, now=1200)
            self.assertEqual(final["manual_event_jobs"], 1)
            self.assertEqual(final["auto_completed_without_manual_event"], 1)
            self.assertAlmostEqual(final["auto_completion_rate"], 1 / 4)

    def test_observation_history_is_bounded_and_has_no_lyric_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number in range(6):
                update_telemetry(root, {"status": "degraded" if number % 2 else "ok", "reasons": [{"code": "source_probe_failed", "private": "SECRET"}]},
                    rollout_started_at=None, now=100000 + number * 86400, daily_retention=2, change_retention=3)
            text = (root / "service-telemetry.json").read_text();state = json.loads(text)
            self.assertEqual(len(state["daily"]), 2);self.assertEqual(len(state["state_changes"]), 3)
            self.assertNotIn("SECRET", text)
            self.assertIsNone(state["metrics"]["completion_rate"])
