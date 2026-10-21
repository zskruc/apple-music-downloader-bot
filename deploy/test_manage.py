"""Maintenance lifecycle tests with synthetic state and a fake Docker daemon."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from deploy import manage


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "installation"
        (self.root / "source").mkdir(parents=True)
        (self.root / "config").mkdir()
        (self.root / "source/compose.yaml").write_text("services: {}\n")
        self.data = self.root / "data/state"
        self.data.mkdir(parents=True)
        (self.root / "config/private.env").write_text("STATE_PATH=" + str(self.data) + "\n")
        self.descriptor = {"schema": 1, "project_name": "fixture-main", "installation_id": "fixture-owner",
                           "source_dir": "source", "env_file": "config/private.env", "compose_files": ["compose.yaml"]}
        self.write_descriptor()
        self.rows = [self.row(name, running=name != "youtube-auth-browser") for name in
                     ("sq-mysql", "legacy-wrapper", "wrapper", "sq-neteasecloudmusicapi",
                      "sqmusic-server", "youtube-auth-browser", "word-lyrics-nas-aligner",
                      "bot", "wrapper-watchdog", "bot-watchdog")]
        self.configs = {"fixture-main": {"services": {row["service"]: {} for row in self.rows}}}
        self.actions = []
        self.owners = {}
        self.stop_failure = None
        self.start_failure = None
        self.on_stop = None
        self.patch(manage.migration, "_compose_config", side_effect=lambda p: self.configs[p["project_name"]])
        self.patch(manage.migration, "_containers", side_effect=lambda: copy.deepcopy(self.rows))
        self.snapshot = self.patch(manage.migration, "snapshot", side_effect=self.fake_snapshot)
        self.patch(manage, "docker", side_effect=self.fake_docker)

    def patch(self, obj, name, **kwargs):
        patcher = mock.patch.object(obj, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def row(self, service, running=True, project="fixture-main"):
        return {"id": project + "-" + service, "project": project, "service": service,
                "running": running, "mounts": []}

    def write_descriptor(self):
        (self.root / "deployment.json").write_text(json.dumps(self.descriptor))

    def fake_docker(self, *arguments):
        self.actions.append(arguments)
        if arguments[0] == "inspect":
            return json.dumps({"io.apple-music-downloader.installation": self.owners.get(arguments[-1], "fixture-owner")})
        identifier = arguments[-1]
        row = next(row for row in self.rows if row["id"] == identifier)
        if arguments[0] == "stop":
            if identifier == self.stop_failure:
                raise RuntimeError("synthetic stop failure")
            row["running"] = False
            if self.on_stop:
                self.on_stop(row)
        elif arguments[0] == "start":
            if identifier == self.start_failure:
                raise RuntimeError("synthetic start failure")
            row["running"] = True
        else:
            raise AssertionError("unexpected Docker action")
        return identifier

    def fake_snapshot(self, root, destination, include_media, include_images):
        names = set(self.configs)
        self.assertFalse(any(row["running"] for row in self.rows if row["project"] in names))
        return {"complete": True, "include_media": include_media, "include_images": include_images}

    def backup(self, **kwargs):
        return manage.backup(self.root, self.base / "private-snapshot", **kwargs)

    def operations(self, name):
        return [command[-1] for command in self.actions if command[0] == name]

    def database(self, relative, table, statuses):
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(path)
        try:
            database.execute("CREATE TABLE " + table + " (status TEXT)")
            database.executemany("INSERT INTO " + table + " VALUES (?)", [(status,) for status in statuses])
            database.commit()
        finally:
            database.close()

    def test_backup_stops_watchdogs_then_writers_and_restarts_only_original_running(self):
        originally_running = {row["id"] for row in self.rows if row["running"]}
        unrelated = self.row("bot", project="unrelated-fixture")
        self.rows.append(unrelated)
        result = self.backup()
        stopped = self.operations("stop")
        self.assertEqual(stopped[:2], ["fixture-main-bot-watchdog", "fixture-main-wrapper-watchdog"])
        self.assertLess(stopped.index("fixture-main-bot"), stopped.index("fixture-main-word-lyrics-nas-aligner"))
        self.assertLess(stopped.index("fixture-main-sqmusic-server"), stopped.index("fixture-main-sq-mysql"))
        self.assertLess(stopped.index("fixture-main-wrapper"), stopped.index("fixture-main-legacy-wrapper"))
        self.assertEqual(set(stopped), originally_running)
        self.assertEqual(self.operations("start"), list(reversed(stopped)))
        self.assertNotIn("fixture-main-youtube-auth-browser", self.operations("start"))
        self.assertTrue(unrelated["running"])
        self.assertTrue(result["include_media"])
        self.assertTrue(result["include_images"])
        ledger = json.loads((self.root / "state/maintenance.json").read_text())
        self.assertTrue(ledger["complete"])
        self.assertFalse(ledger["services_left_stopped"])

    def test_migration_cli_keeps_all_writers_stopped_after_verified_snapshot(self):
        with mock.patch('builtins.print') as output:
            result = manage.main(['backup', '--root', str(self.root), '--destination',
                                  str(self.base / 'private-snapshot'), '--leave-stopped'])
        self.assertEqual(result, 0)
        self.assertFalse(self.operations('start'))
        self.assertFalse(any(row['running'] for row in self.rows))
        self.assertTrue(json.loads((self.root / 'state/maintenance.json').read_text())['services_left_stopped'])
        self.assertIn('remain stopped', output.call_args.args[0])

    def test_snapshot_failure_restores_original_running_set(self):
        original = {row["id"] for row in self.rows if row["running"]}
        self.snapshot.side_effect = manage.migration.MigrationError("synthetic incomplete snapshot")
        with self.assertRaisesRegex(manage.migration.MigrationError, "incomplete"):
            self.backup()
        self.assertEqual(set(self.operations("start")), original)
        self.assertTrue(all(row["running"] for row in self.rows if row["id"] in original))
        self.assertFalse(json.loads((self.root / "state/maintenance.json").read_text())["complete"])

    def test_stop_failure_does_not_snapshot_and_attempts_recovery(self):
        original = {row["id"] for row in self.rows if row["running"]}
        self.stop_failure = "fixture-main-bot"
        with self.assertRaisesRegex(RuntimeError, "stop failure"):
            self.backup()
        self.snapshot.assert_not_called()
        self.assertEqual(set(self.operations("start")), original)

    def test_explicit_no_restart_still_restores_after_failed_snapshot(self):
        self.snapshot.side_effect = manage.migration.MigrationError("synthetic failure")
        with self.assertRaises(manage.migration.MigrationError):
            self.backup(restart=False)
        self.assertEqual(self.operations("start"), list(reversed(self.operations("stop"))))

    def test_explicit_no_restart_leaves_completed_snapshot_stopped(self):
        result = self.backup(restart=False, include_media=False, include_images=False)
        self.assertTrue(result["complete"])
        self.assertFalse(result["include_media"])
        self.assertFalse(result["include_images"])
        self.assertFalse(self.operations("start"))
        self.assertTrue(json.loads((self.root / "state/maintenance.json").read_text())["services_left_stopped"])

    def test_foreign_owner_is_rejected_before_any_stop(self):
        self.owners["fixture-main-bot"] = "other-fixture-owner"
        with self.assertRaisesRegex(RuntimeError, "another installation"):
            self.backup()
        self.assertFalse(self.operations("stop"))
        self.assertFalse(self.operations("start"))
        self.snapshot.assert_not_called()

    def test_undeclared_service_or_absent_project_is_rejected_without_stop(self):
        self.rows.append(self.row("unknown-service"))
        with self.assertRaisesRegex(RuntimeError, "undeclared service"):
            self.backup()
        self.assertFalse(self.operations("stop"))
        self.rows.clear()
        with self.assertRaisesRegex(RuntimeError, "container evidence"):
            self.backup()
        self.assertFalse(self.operations("stop"))

    def test_active_bot_database_is_rejected_without_stop(self):
        self.database("task-cards.sqlite3", "task_cards", ["queued", "running"])
        with self.assertRaisesRegex(RuntimeError, "Active bot or lyric"):
            self.backup()
        self.assertFalse(self.operations("stop"))
        self.snapshot.assert_not_called()

    def test_leased_lyric_job_is_rejected_without_stop(self):
        self.database("word-lyrics/automation/queue.sqlite3", "jobs", ["waiting", "leased_alignment"])
        with self.assertRaisesRegex(RuntimeError, "Active bot or lyric"):
            self.backup()
        self.assertFalse(self.operations("stop"))

    def test_queued_only_database_can_be_snapshotted_without_changing_status(self):
        self.database("task-cards.sqlite3", "task_cards", ["queued", "done"])
        self.backup()
        database = sqlite3.connect(self.data / "task-cards.sqlite3")
        try:
            self.assertEqual(database.execute("SELECT status FROM task_cards").fetchall(), [("queued",), ("done",)])
        finally:
            database.close()

    def test_late_active_job_after_stopping_aborts_snapshot_and_recovers(self):
        self.on_stop = lambda row: self.database("task-cards.sqlite3", "task_cards", ["running"]) if row["service"] == "sq-mysql" else None
        with self.assertRaisesRegex(RuntimeError, "Active bot or lyric"):
            self.backup()
        self.snapshot.assert_not_called()
        self.assertEqual(self.operations("start"), list(reversed(self.operations("stop"))))

    def test_restart_failure_is_recorded_and_other_restarts_continue(self):
        self.start_failure = "fixture-main-bot"
        with self.assertRaisesRegex(RuntimeError, "manual restart: bot"):
            self.backup()
        ledger = json.loads((self.root / "state/maintenance.json").read_text())
        self.assertTrue(ledger["complete"])
        self.assertEqual(ledger["restart_failures"], ["bot"])
        self.assertEqual(self.operations("start"), list(reversed(self.operations("stop"))))

    def test_task_writer_without_state_is_rejected_before_stop(self):
        (self.root / "config/private.env").write_text("FIXTURE=true\n")
        with self.assertRaisesRegex(RuntimeError, "task-writing project"):
            self.backup()
        self.assertFalse(self.operations("stop"))

    def test_independent_lx_sq_projects_without_bot_state_are_supported(self):
        self.descriptor = {"schema": 1, "projects": []}
        self.rows = []
        self.configs = {}
        for identifier, service in (("lx", "lxserver"), ("sq", "sqmusic-server")):
            directory = self.root / identifier
            directory.mkdir()
            (directory / "private.env").write_text("FIXTURE=true\n")
            (directory / "compose.yaml").write_text("services: {}\n")
            project = "fixture-" + identifier
            self.descriptor["projects"].append({"id": identifier, "project_name": project,
                "project_dir": identifier, "source_dir": ".", "env_file": "private.env", "compose_files": ["compose.yaml"]})
            self.rows.append(self.row(service, project=project))
            self.configs[project] = {"services": {service: {}}}
        self.write_descriptor()
        self.assertTrue(self.backup()["complete"])
        self.assertEqual(set(self.operations("start")), {row["id"] for row in self.rows})


if __name__ == "__main__":
    unittest.main()
