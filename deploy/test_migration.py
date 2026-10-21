from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import socket
import tarfile
import tempfile
import unittest
from unittest import mock

from deploy import migration as migration


class SnapshotFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.install = self.root / "installed"
        (self.install / "source").mkdir(parents=True)
        (self.install / "config").mkdir()
        (self.install / "source/compose.yaml").write_text("services: {}\n")
        (self.install / "config/private.env").write_text("SYNTHETIC_SECRET=private-fixture-value\n")
        (self.install / "config/installer.compose.json").write_text('{"services":{}}')
        self.state, self.profile, self.media, self.mysql = [self.root / name for name in ("state", "browser", "media", "mysql")]
        for directory in (self.state, self.profile, self.media, self.mysql):
            directory.mkdir()
        (self.state / "cookies.txt").write_text("synthetic Cookie fixture, never used for authentication")
        (self.state / "cookies.txt").chmod(0o600)
        (self.profile / "Cookies").write_bytes(b"synthetic browser database")
        (self.media / "song.flac").write_bytes(b"synthetic media fixture")
        (self.mysql / "database.dat").write_bytes(b"synthetic stopped database fixture")
        with closing(sqlite3.connect(self.state / "tasks.sqlite3")) as database:
            database.execute("CREATE TABLE tasks (id TEXT, status TEXT)")
            database.execute("INSERT INTO tasks VALUES ('unchanged-id', 'running')")
            database.commit()
        self.descriptor = {"schema": 1, "project_name": "fixture-suite", "source_dir": "source",
                           "env_file": "config/private.env", "compose_files": ["compose.yaml"],
                           "extra_compose_files": ["config/installer.compose.json"],
                           "profiles": ["auth", "lx", "sq"], "media_paths": [str(self.media)]}
        self.write_descriptor()
        self.config = {"services": {
            "bot": {"volumes": [{"type": "bind", "source": str(self.state), "target": "/state"},
                                 {"type": "bind", "source": str(self.media), "target": "/music"},
                                 {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}]},
            "browser": {"volumes": [{"type": "bind", "source": str(self.profile), "target": "/config"}]},
            "mysql": {"volumes": [{"type": "volume", "source": "mysql-data", "target": "/var/lib/mysql"}]},
        }, "volumes": {"mysql-data": {"name": "fixture-suite_mysql-data"}}}
        self.rows = [{"id": "fixture-container-" + service, "project": "fixture-suite", "service": service,
                      "running": False, "image_id": "sha256:" + "a" * 64, "image_ref": "fixture/image@sha256:" + "a" * 64,
                      "mounts": [], "working_dir": str(self.install / "source"), "config_files": "fixture-only"}
                     for service in self.config["services"]]
        self.addCleanup(mock.patch.stopall)
        self.config_mock = mock.patch.object(migration, "_compose_config", return_value=self.config).start()
        self.container_mock = mock.patch.object(migration, "_containers", side_effect=lambda: copy.deepcopy(self.rows)).start()
        mock.patch.object(migration, "_volume_path", return_value=("fixture-suite_mysql-data", self.mysql)).start()
        self.bundle = self.root / "bundle"

    def write_descriptor(self):
        (self.install / "deployment.json").write_text(json.dumps(self.descriptor))

    def snapshot(self, **kwargs):
        return migration.snapshot(self.install, self.bundle, **kwargs)

    def update_manifest(self, manifest):
        (self.bundle / "manifest.json").write_text(json.dumps(manifest))

    def refresh_payload_hash(self, manifest):
        digest, size = migration._hash_file(self.bundle / "payload.tar")
        manifest["archive"].update(sha256=digest, size=size)
        self.update_manifest(manifest)


class MySQLRuntimeSnapshotTests(SnapshotFixture):
    def observed_mysql_mount(self):
        row = next(row for row in self.rows if row["service"] == "mysql")
        row["mounts"] = [{"type": "volume", "source": str(self.mysql),
                          "target": "/var/lib/mysql", "read_only": False}]

    def test_offline_mysql_socket_alias_is_ledgered_without_losing_database_bytes(self):
        self.observed_mysql_mount()
        alias = self.mysql / "mysql.sock"
        alias.symlink_to(migration.MYSQL_SOCKET_ALIAS)
        database = (self.mysql / "database.dat").read_bytes()
        manifest = self.snapshot()
        omissions = manifest["coverage"]["runtime_omissions"]
        self.assertEqual(len(omissions), 1)
        omission = omissions[0]
        self.assertEqual(omission["source"], str(alias))
        self.assertEqual(omission["kind"], "symlink")
        self.assertEqual(omission["link"], "/var/run/mysqld/mysqld.sock")
        self.assertEqual(omission["target"], "/var/lib/mysql/mysql.sock")
        self.assertEqual(omission["reason"], "mysql-runtime-socket")
        self.assertEqual(os.readlink(alias), migration.MYSQL_SOCKET_ALIAS)
        self.assertNotIn(omission["path"], [item["path"] for item in manifest["files"]])
        with tarfile.open(self.bundle / "payload.tar") as payload:
            self.assertNotIn(omission["path"], payload.getnames())
        result = migration.restore_bundle(self.bundle, self.root / "new-host")
        restored = Path(result["source_map"][str(self.mysql)])
        self.assertEqual((restored / "database.dat").read_bytes(), database)
        self.assertFalse((restored / "mysql.sock").exists())
        self.assertFalse((restored / "mysql.sock").is_symlink())
        self.assertEqual(result["runtime_omissions"], omissions)

    def test_stopped_mysql_stale_unix_socket_is_omitted_not_restored(self):
        self.observed_mysql_mount()
        # A relative bind avoids platform AF_UNIX pathname-length limits.
        previous = os.getcwd()
        with socket.socket(socket.AF_UNIX) as stale:
            try:
                os.chdir(self.mysql)
                stale.bind("mysql.sock")
            finally:
                os.chdir(previous)
        manifest = self.snapshot()
        self.assertEqual(manifest["coverage"]["runtime_omissions"][0]["kind"], "socket")
        self.assertTrue((self.mysql / "mysql.sock").exists())
        result = migration.restore_bundle(self.bundle, self.root / "new-host")
        restored = Path(result["source_map"][str(self.mysql)])
        self.assertFalse((restored / "mysql.sock").exists())
        self.assertEqual((restored / "database.dat").read_bytes(), (self.mysql / "database.dat").read_bytes())

    def test_ordinary_file_named_mysql_socket_is_preserved(self):
        self.observed_mysql_mount()
        (self.mysql / "mysql.sock").write_bytes(b"ordinary persistent file must survive")
        manifest = self.snapshot()
        self.assertEqual(manifest["coverage"]["runtime_omissions"], [])
        result = migration.restore_bundle(self.bundle, self.root / "new-host")
        restored = Path(result["source_map"][str(self.mysql)])
        self.assertEqual((restored / "mysql.sock").read_bytes(), b"ordinary persistent file must survive")

    def test_other_external_links_are_never_treated_as_runtime_sockets(self):
        self.observed_mysql_mount()
        outside = self.root / "outside"
        outside.write_text("persistent outside fixture")
        for directory, name, target in ((self.mysql, "mysql.sock", str(outside)),
                                        (self.mysql, "other.sock", migration.MYSQL_SOCKET_ALIAS),
                                        (self.state, "mysql.sock", migration.MYSQL_SOCKET_ALIAS)):
            with self.subTest(directory=directory.name, name=name):
                alias = directory / name
                alias.symlink_to(target)
                try:
                    with self.assertRaises((migration.MigrationError, FileNotFoundError)):
                        self.snapshot()
                    self.assertFalse(self.bundle.exists())
                finally:
                    alias.unlink()

    def test_alias_requires_actual_stopped_container_data_mount_evidence(self):
        alias = self.mysql / "mysql.sock"
        alias.symlink_to(migration.MYSQL_SOCKET_ALIAS)
        with self.assertRaises((migration.MigrationError, FileNotFoundError)):
            self.snapshot()
        self.observed_mysql_mount()
        next(row for row in self.rows if row["service"] == "mysql")["running"] = True
        with self.assertRaisesRegex(migration.MigrationError, "stopped"):
            self.snapshot()
        self.assertFalse(self.bundle.exists())

    def test_omitted_alias_replacement_or_disappearance_prevents_publication(self):
        self.observed_mysql_mount()
        alias = self.mysql / "mysql.sock"
        original = migration._write_payload
        for action in ("replace", "remove"):
            with self.subTest(action=action):
                alias.symlink_to(migration.MYSQL_SOCKET_ALIAS)
                def mutate(*args):
                    original(*args)
                    alias.unlink()
                    if action == "replace":
                        alias.symlink_to(migration.MYSQL_SOCKET_ALIAS)
                with mock.patch.object(migration, "_write_payload", side_effect=mutate):
                    with self.assertRaisesRegex(migration.MigrationError, "runtime socket"):
                        self.snapshot()
                self.assertFalse(self.bundle.exists())
                alias.unlink(missing_ok=True)

    def test_tampered_runtime_omission_ledger_is_rejected_before_restore(self):
        self.observed_mysql_mount()
        (self.mysql / "mysql.sock").symlink_to(migration.MYSQL_SOCKET_ALIAS)
        manifest = self.snapshot()
        manifest["coverage"]["runtime_omissions"][0]["link"] = "/another/socket"
        self.update_manifest(manifest)
        target = self.root / "new-host"
        with self.assertRaisesRegex(migration.MigrationError, "MySQL socket alias"):
            migration.restore_bundle(self.bundle, target)
        self.assertFalse(target.exists())


class OfflineSnapshotTests(SnapshotFixture):
    def test_real_sq_prepare_after_snapshot_restore_keeps_isolated_auth_and_database_credentials(self):
        from deploy import installer
        service_names = {"browser": "youtube-auth-browser", "mysql": "sq-mysql"}
        for old, new in service_names.items():
            self.config["services"][new] = self.config["services"].pop(old)
        for row in self.rows:
            row["service"] = service_names.get(row["service"], row["service"])
        staging = self.root / "sq-staging"
        staging.mkdir()
        auth = self.root / "sq-secrets/sq-auth.json"
        auth.parent.mkdir()
        auth.write_bytes(b'{ "username": "fixture-sq-user", "password": "fixture-original-auth" }\n')
        lx_auth = self.root / "lx-secrets/frontend-auth"
        lx_auth.parent.mkdir()
        lx_auth.write_bytes(b"fixture-original-lx-auth\n")
        self.config["services"]["sqmusic-server"] = {"volumes": [
            {"type": "bind", "source": str(staging), "target": "/music"},
            {"type": "bind", "source": str(auth), "target": "/run/secrets/sq-auth.json", "read_only": True}]}
        self.config["services"]["bot"]["volumes"].append(
            {"type": "bind", "source": str(lx_auth), "target": "/run/secrets/lx-auth", "read_only": True})
        self.rows.append({**self.rows[0], "id": "fixture-sq-server", "service": "sqmusic-server"})
        managed = {"SQ_STAGING_PATH": str(staging), "SQ_MYSQL_DATA_PATH": str(self.mysql),
                   "SQ_AUTH_PATH": str(auth), "LX_FRONTEND_AUTH_PATH": str(lx_auth), "MUSIC_PATH": str(self.media)}
        self.descriptor["managed_paths"] = managed
        self.write_descriptor()
        credentials = {"SQ_DB_PASSWORD": "fixture-original-db$literal", "SQ_DB_ROOT_PASSWORD": "fixture-original-root",
                       "SQ_JWT_SECRET": "fixture-original-jwt"}
        installer.write_env(self.install / "config/private.env", {**managed, **credentials})
        original_db = (self.mysql / "database.dat").read_bytes()
        original_auth = auth.read_bytes()
        self.snapshot(include_media=True)
        target = self.root / "new-host"
        restored = migration.restore_bundle(self.bundle, target)
        (target / "source").mkdir()
        (target / "source/.env.example").write_text("")
        subject = installer.Installer(target)
        subject.state["restore"] = restored
        subject.state["restored_needs_cutover"] = True
        subject.compose = mock.Mock()
        subject.stage_configuration()
        values = installer.read_env(subject.env_file)
        restored_auth = Path(values["SQ_AUTH_PATH"])
        restored_db = Path(values["SQ_MYSQL_DATA_PATH"])
        restored_staging = Path(values["SQ_STAGING_PATH"])
        spec = importlib.util.spec_from_file_location("migration_sq_contract", Path(__file__).parents[1] / "services/sq-music/bootstrap.py")
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        before_env = subject.env_file.read_bytes()
        with mock.patch.object(bootstrap.secrets, "token_hex", side_effect=AssertionError("must not regenerate restored secrets")), \
                mock.patch.object(bootstrap.secrets, "token_urlsafe", side_effect=AssertionError("must not regenerate restored auth")), \
                mock.patch("sys.stdout", new=io.StringIO()):
            bootstrap.prepare(subject.env_file)
        self.assertEqual(restored_auth.name, auth.name)
        self.assertNotIn(restored_auth.parent, restored_db.parents)
        self.assertNotIn(restored_auth.parent, restored_staging.parents)
        self.assertEqual(Path(values["LX_FRONTEND_AUTH_PATH"]).name, lx_auth.name)
        self.assertEqual(Path(values["LX_FRONTEND_AUTH_PATH"]).read_bytes(), lx_auth.read_bytes())
        self.assertEqual(subject.env_file.read_bytes(), before_env)
        self.assertEqual(restored_auth.read_bytes(), original_auth)
        self.assertEqual((restored_db / "database.dat").read_bytes(), original_db)
        self.assertEqual({key: installer.read_env(subject.env_file)[key] for key in credentials}, credentials)
        self.assertEqual(restored_auth.stat().st_mode & 0o777, 0o600)
        self.assertEqual(restored_auth.parent.stat().st_mode & 0o777, 0o700)

    def test_complete_snapshot_and_restore_preserve_private_bytes_and_task_state(self):
        manifest = self.snapshot()
        self.assertTrue(manifest["complete"])
        self.assertNotIn("private-fixture-value", json.dumps(manifest))
        self.assertEqual((self.bundle / "payload.tar").stat().st_mode & 0o777, 0o600)
        target = self.root / "new-host"
        (target / "source").mkdir(parents=True)
        (target / "state").mkdir()
        (target / "state/install.lock").write_text("fixture lock")
        result = migration.restore_bundle(self.bundle, target)
        restored_state = Path(result["source_map"][str(self.state)])
        self.assertEqual((restored_state / "cookies.txt").read_bytes(), (self.state / "cookies.txt").read_bytes())
        with closing(sqlite3.connect(restored_state / "tasks.sqlite3")) as database:
            self.assertEqual(database.execute("SELECT id,status FROM tasks").fetchall(), [("unchanged-id", "running")])
        self.assertEqual(Path(result["env_files"]["main"]).read_text(), "SYNTHETIC_SECRET=private-fixture-value\n")
        self.assertTrue((target / "config/restored/main/overrides/config/installer.compose.json").is_file())
        self.assertFalse((target / "config/private.env").exists())
        self.assertEqual(list(Path(result["source_map"][str(self.media)]).iterdir()), [])
        self.assertEqual(len(result["omitted_sources"]), 1)
        self.assertEqual((target / "state/install.lock").read_text(), "fixture lock")

    def test_include_media_copies_content_and_coverage_is_explicit(self):
        manifest = self.snapshot(include_media=True)
        self.assertFalse(any(item.get("reason") == "media-not-requested" for item in manifest["coverage"]["omitted_mounts"]))
        result = migration.restore_bundle(self.bundle, self.root / "new-host")
        self.assertEqual((Path(result["source_map"][str(self.media)]) / "song.flac").read_bytes(), b"synthetic media fixture")

    def test_running_selected_project_is_refused_without_any_service_action(self):
        self.rows[0]["running"] = True
        with self.assertRaisesRegex(migration.MigrationError, "stopped"):
            self.snapshot()
        self.assertFalse(self.bundle.exists())

    def test_other_running_project_sharing_data_is_refused(self):
        self.rows.append({"project": "another-project", "running": True,
                          "mounts": [{"source": str(self.state), "read_only": False}]})
        with self.assertRaisesRegex(migration.MigrationError, "Another running"):
            self.snapshot()
        self.assertFalse(self.bundle.exists())

    def test_missing_source_never_produces_a_complete_bundle(self):
        self.config["services"]["bot"]["volumes"][0]["source"] = str(self.root / "missing")
        with self.assertRaisesRegex(migration.MigrationError, "missing"):
            self.snapshot()
        self.assertFalse(self.bundle.exists())

    def test_missing_omitted_media_source_still_fails_coverage(self):
        self.config["services"]["bot"]["volumes"][1]["source"] = str(self.root / "missing-media")
        with self.assertRaisesRegex(migration.MigrationError, "missing"):
            self.snapshot(include_media=False)
        self.assertFalse(self.bundle.exists())

    def test_unverifiable_project_name_is_refused(self):
        self.rows = []
        with self.assertRaisesRegex(migration.MigrationError, "container evidence"):
            self.snapshot()

    def test_external_symlink_and_hardlink_and_fifo_are_refused(self):
        outside = self.root / "outside"
        outside.write_text("outside data")
        bad = self.state / "external"
        bad.symlink_to(outside)
        with self.assertRaisesRegex(migration.MigrationError, "symlinks"):
            self.snapshot()
        bad.unlink()
        os.link(outside, bad)
        with self.assertRaisesRegex(migration.MigrationError, "Hard-linked"):
            self.snapshot()
        bad.unlink()
        os.mkfifo(bad)
        with self.assertRaisesRegex(migration.MigrationError, "special"):
            self.snapshot()

    def test_internal_symlink_is_recreated_only_inside_its_verified_data_root(self):
        (self.state / "cookie-copy").symlink_to("cookies.txt")
        self.snapshot()
        result = migration.restore_bundle(self.bundle, self.root / "new-host")
        link = Path(result["source_map"][str(self.state)]) / "cookie-copy"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_text(), (self.state / "cookies.txt").read_text())

    def test_source_change_during_snapshot_prevents_publication(self):
        original = migration._write_payload
        def mutate(*args):
            original(*args)
            (self.state / "new-file").write_text("concurrent fixture writer")
        with mock.patch.object(migration, "_write_payload", side_effect=mutate):
            with self.assertRaisesRegex(migration.MigrationError, "changed"):
                self.snapshot()
        self.assertFalse(self.bundle.exists())
        self.assertFalse(list(self.root.glob(".amdl-snapshot-*")))

    def test_project_restarting_before_completion_prevents_publication(self):
        stopped = copy.deepcopy(self.rows)
        running = copy.deepcopy(self.rows)
        running[0]["running"] = True
        self.container_mock.side_effect = [stopped, stopped, running]
        with self.assertRaisesRegex(migration.MigrationError, "stopped"):
            self.snapshot()
        self.assertFalse(self.bundle.exists())

    def test_named_volume_is_included_and_preserves_original_target_mapping(self):
        self.snapshot()
        result = migration.restore_bundle(self.bundle, self.root / "new-host")
        mount = next(item for item in result["mounts"] if item.get("volume_name"))
        self.assertEqual(mount["target"], "/var/lib/mysql")
        self.assertEqual((Path(mount["restored_path"]) / "database.dat").read_bytes(), b"synthetic stopped database fixture")

    def test_declared_parent_paths_preserve_lx_layout_for_initialization_and_compose(self):
        parent = self.root / "lx-backend"
        children = []
        for name in ("data", "cache", "logs", "cover_cache"):
            directory = parent / name
            directory.mkdir(parents=True)
            children.append({"type": "bind", "source": str(directory), "target": "/server/" + name})
        (parent / "data/config.js").write_text("synthetic existing LX configuration")
        self.descriptor["managed_paths"] = {"LX_DATA_PATH": str(parent)}
        self.write_descriptor()
        self.config["services"]["lx"] = {"volumes": children}
        self.rows.append({**self.rows[0], "id": "fixture-lx", "service": "lx"})
        self.snapshot()
        result = migration.restore_bundle(self.bundle, self.root / "new-host")
        new_parent = Path(result["source_map"][str(parent)])
        self.assertEqual((new_parent / "data/config.js").read_text(), "synthetic existing LX configuration")
        for child in children:
            expected = new_parent / Path(child["source"]).name
            self.assertEqual(Path(result["source_map"][child["source"]]), expected)
            self.assertTrue(expected.is_dir())

    def test_image_export_is_opt_in_and_restore_does_not_load_images(self):
        def export(command):
            self.assertEqual(command[:3], ["docker", "image", "save"])
            Path(command[command.index("--output") + 1]).write_bytes(b"synthetic image export fixture")
            return ""
        with mock.patch.object(migration, "_run", side_effect=export) as run:
            self.snapshot(include_images=True)
            result = migration.restore_bundle(self.bundle, self.root / "new-host")
        self.assertEqual(run.call_count, 1)
        self.assertTrue(result["images_included"])
        self.assertEqual(Path(result["image_archive"]).read_bytes(), b"synthetic image export fixture")

    def test_multiple_projects_are_explicit_and_inventory_is_read_only(self):
        second = self.root / "second-project"
        second.mkdir()
        (second / "compose.yaml").write_text("services: {}")
        (second / ".env").write_text("SYNTHETIC_SECOND=fixture")
        self.descriptor["projects"] = [dict(self.descriptor, id="downloads"), {
            "id": "lx", "project_name": "fixture-lx", "project_dir": str(second), "source_dir": ".",
            "env_file": ".env", "compose_files": ["compose.yaml"]}]
        self.write_descriptor()
        self.config_mock.side_effect = lambda project: self.config if project["id"] == "downloads" else {"services": {"lx": {"volumes": []}}}
        self.rows.append({**self.rows[0], "id": "fixture-lx-container", "project": "fixture-lx", "service": "lx"})
        manifest = self.snapshot()
        self.assertEqual({row["project_name"] for row in manifest["projects"]}, {"fixture-suite", "fixture-lx"})
        read_only = migration.inventory(["fixture-lx"])
        self.assertEqual(set(read_only["projects"]), {"fixture-lx"})
        self.assertFalse(read_only["service_changes"])


class RestoreValidationTests(SnapshotFixture):
    def test_service_plan_is_validated_before_any_destination_write(self):
        self.snapshot()
        target = self.root / "new-host"
        def refuse(plan):
            self.assertTrue(plan["mounts"])
            raise migration.MigrationError("synthetic unknown service refusal")
        with self.assertRaisesRegex(migration.MigrationError, "unknown service"):
            migration.restore_bundle(self.bundle, target, plan_validator=refuse)
        self.assertFalse(target.exists())

    def test_dry_run_does_not_create_new_destination(self):
        self.snapshot()
        target = self.root / "new-host"
        result = migration.restore_bundle(self.bundle, target, dry_run=True)
        self.assertFalse(target.exists())
        self.assertEqual(len(result["source_map"]), 4)

    def test_corrupt_archive_fails_before_any_target_write(self):
        self.snapshot()
        with (self.bundle / "payload.tar").open("ab") as handle:
            handle.write(b"tampered")
        target = self.root / "new-host"
        with self.assertRaisesRegex(migration.MigrationError, "SHA-256"):
            migration.restore_bundle(self.bundle, target)
        self.assertFalse(target.exists())

    def test_file_digest_is_checked_independently_of_the_archive_digest(self):
        manifest = self.snapshot()
        record = next(item for item in manifest["files"] if item["kind"] == "file")
        record["sha256"] = "0" * 64
        self.update_manifest(manifest)
        target = self.root / "new-host"
        with self.assertRaisesRegex(migration.MigrationError, "payload file failed SHA-256"):
            migration.restore_bundle(self.bundle, target)
        self.assertFalse(target.exists())

    def test_external_symlink_in_a_forged_ledger_is_refused_before_writing(self):
        manifest = self.snapshot()
        manifest["files"].append({"path": "data/s0000/escape", "kind": "symlink", "link": "../../../outside",
                                  "mode": 0o777, "size": 0, "mtime_ns": 0, "uid": 0, "gid": 0})
        self.update_manifest(manifest)
        target = self.root / "new-host"
        with self.assertRaisesRegex(migration.MigrationError, "leaves its data source"):
            migration.restore_bundle(self.bundle, target)
        self.assertFalse(target.exists())

    def test_populated_or_symlink_destination_is_refused_without_overwrite(self):
        self.snapshot()
        target = self.root / "new-host"
        (target / "data").mkdir(parents=True)
        (target / "data/existing").write_text("keep")
        with self.assertRaisesRegex(migration.MigrationError, "new or empty"):
            migration.restore_bundle(self.bundle, target)
        self.assertEqual((target / "data/existing").read_text(), "keep")
        shutil_target = self.root / "alias"
        shutil_target.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(migration.MigrationError, "symlinks"):
            migration.restore_bundle(self.bundle, shutil_target)

    def test_unlisted_tar_path_traversal_is_refused_even_if_archive_digest_matches(self):
        manifest = self.snapshot()
        with tarfile.open(self.bundle / "payload.tar", "a") as archive:
            member = tarfile.TarInfo("../../escaped")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
        self.refresh_payload_hash(manifest)
        with self.assertRaisesRegex(migration.MigrationError, "Unsafe"):
            migration.restore_bundle(self.bundle, self.root / "new-host")
        self.assertFalse((self.root / "escaped").exists())

    def test_hardlink_tar_member_is_refused(self):
        manifest = self.snapshot()
        name = "data/s0000/forged-hardlink"
        manifest["files"].append({"path": name, "kind": "file", "mode": 0o600, "uid": 0, "gid": 0,
                                  "mtime_ns": 0, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()})
        with tarfile.open(self.bundle / "payload.tar", "a") as archive:
            member = tarfile.TarInfo(name)
            member.type, member.linkname = tarfile.LNKTYPE, "data/s0000/cookies.txt"
            archive.addfile(member)
        self.refresh_payload_hash(manifest)
        with self.assertRaisesRegex(migration.MigrationError, "Hard links"):
            migration.restore_bundle(self.bundle, self.root / "new-host")

    def test_missing_source_coverage_is_refused(self):
        manifest = self.snapshot()
        manifest["sources"] = []
        self.update_manifest(manifest)
        with self.assertRaisesRegex(migration.MigrationError, "coverage|mismatch"):
            migration.restore_bundle(self.bundle, self.root / "new-host")

    def test_invalid_overlay_path_is_refused_before_docker(self):
        self.descriptor["extra_compose_files"] = ["../outside.compose.json"]
        self.write_descriptor()
        with self.assertRaisesRegex(migration.MigrationError, "Unsafe"):
            self.snapshot()
        self.config_mock.assert_not_called()

    def test_unlisted_bundle_file_is_refused(self):
        self.snapshot()
        (self.bundle / "unverified-extra").write_text("fixture")
        with self.assertRaisesRegex(migration.MigrationError, "unlisted"):
            migration.validate_bundle(self.bundle)


if __name__ == "__main__":
    unittest.main()
