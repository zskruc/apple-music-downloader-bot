"""Synthetic installer tests: no daemon, network, real tokens or accounts."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import tempfile
import unittest
from unittest import mock

from deploy import installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "installation"
        self.calls = []

    def runner(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    def subject(self, **kwargs):
        return installer.Installer(self.root, runner=self.runner, **kwargs)

    def fixture_source(self):
        source = self.root / "source"
        source.mkdir(parents=True)
        (source / ".env.example").write_text("TELEGRAM_BOT_TOKEN=\nSYNOLOGY_CHAT_ENABLED=false\n")
        return source

    def test_dry_run_has_no_files_commands_or_secret_output(self):
        config = self.base / "input.json"
        config.write_text(json.dumps({"env": {"TELEGRAM_BOT_TOKEN": "synthetic-private"}}))
        plan = self.subject(config=config, dry_run=True).install()
        self.assertFalse(self.root.exists())
        self.assertFalse(self.calls)
        self.assertNotIn("synthetic-private", json.dumps(plan))
        self.assertFalse(plan["containers_changed"])

    def test_relative_root_and_symlink_ancestors_are_rejected(self):
        with self.assertRaises(ValueError):
            installer.validate_root(Path("relative"))
        target = self.base / "real"
        target.mkdir()
        link = self.base / "alias"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            installer.validate_root(link / "new")

    def test_shared_storage_and_existing_git_checkout_are_refused(self):
        for shared in (Path("/volume1/docker"), Path("/Users/example/LocalWork"), Path("/opt")):
            with self.subTest(shared=shared), self.assertRaises(ValueError):
                installer.validate_root(shared)
        checkout = self.base / "existing-checkout"
        checkout.mkdir()
        (checkout / ".git").write_text("gitdir: synthetic-worktree\n")
        with self.assertRaises(ValueError):
            installer.validate_root(checkout)
        self.assertEqual(list(checkout.iterdir()), [checkout / ".git"])

    def test_config_accepts_scalar_env_and_rejects_multiline_injection(self):
        config = self.base / "private.json"
        config.write_text('{"env":{"YT_ENABLED":true,"PUID":1234}}')
        self.assertEqual(installer.load_config(config), {"YT_ENABLED": "true", "PUID": "1234"})
        config.write_text(json.dumps({"env": {"TOKEN": "one\nINJECTED=two"}}))
        with self.assertRaises(ValueError):
            installer.load_config(config)

    def test_private_env_roundtrip_permissions_and_literal_dollars(self):
        path = self.root / "config/private.env"
        values = {"TOKEN": "literal$variable#fragment", "PASSWORD": "single'quote"}
        installer.write_env(path, values)
        self.assertEqual(installer.read_env(path), values)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_host_shell_cannot_override_managed_compose_paths_or_credentials(self):
        self.fixture_source()
        subject = self.subject()
        installer.write_env(subject.env_file, {"MUSIC_PATH": str(self.root / "media/music"), "SQ_DB_PASSWORD": "synthetic-db"})
        polluted = {"MUSIC_PATH": "/old-production/media", "COMPOSE_PROJECT_NAME": "old-production",
                    "SQ_DB_PASSWORD": "wrong-host-db", "TELEGRAM_BOT_TOKEN": "synthetic-host-token",
                    "DOCKER_HOST": "unix:///var/run/docker.sock", "HTTPS_PROXY": "http://host-network.example:8080"}
        with mock.patch.dict(os.environ, polluted):
            subject.compose("config", "--quiet")
            subject.run(["python3", "bootstrap.py", "configure"], "sq-helper")
        for _command, options in self.calls:
            environment = options["env"]
            self.assertFalse({"MUSIC_PATH", "COMPOSE_PROJECT_NAME", "SQ_DB_PASSWORD", "TELEGRAM_BOT_TOKEN"}.intersection(environment))
            self.assertEqual(environment["DOCKER_HOST"], polluted["DOCKER_HOST"])
            self.assertEqual(environment["HTTPS_PROXY"], polluted["HTTPS_PROXY"])

    def test_restore_mapping_validates_before_restore_writes_and_persists_for_resume(self):
        config = self.base / "private.json"
        config.write_text(json.dumps({"env": {}, "restore_services": {"old-vm": "legacy-wrapper"}}))
        subject = self.subject(config=config, restore=self.base / "bundle")
        plan = {"mounts": [{"service": "old-vm", "target": "/data", "restored_path": str(self.root / "data/vm")}],
                "images_included": True, "image_archive": str(self.root / "data/_images/images.tar"),
                "images": [{"service": "old-vm", "image_id": "sha256:" + "1" * 64}]}
        from deploy import migration
        def restore(bundle, root, *, plan_validator):
            self.assertEqual(root, self.root)
            self.assertFalse(self.root.exists())
            return plan_validator(plan)
        with mock.patch.object(migration, "restore_bundle", side_effect=restore):
            subject.stage_restore()
        restored = subject.state["restore"]
        self.assertEqual(restored["mounts"][0]["service"], "legacy-wrapper")
        self.assertEqual(restored["mounts"][0]["original_service"], "old-vm")
        self.assertTrue(subject.state["restored_needs_cutover"])
        resumed = self.subject()
        self.assertEqual(resumed.normalize_restore(restored)["mounts"][0]["service"], "legacy-wrapper")

    def test_unknown_restore_service_is_rejected_by_prewrite_validator(self):
        from deploy import migration, restore_plan
        subject = self.subject(restore=self.base / "bundle")
        def restore(bundle, root, *, plan_validator):
            return plan_validator({"mounts": [{"service": "unreviewed-vm", "target": "/data", "restored_path": str(root / "data/vm")}], "images": []})
        with mock.patch.object(migration, "restore_bundle", side_effect=restore), \
                self.assertRaises(restore_plan.RestorePlanError):
            subject.stage_restore()
        self.assertFalse(self.root.exists())
        self.assertNotIn("restore", subject.state)

    def test_source_copy_omits_nested_symlinks_env_and_dependencies(self):
        origin = self.base / "checkout"
        (origin / "bot").mkdir(parents=True)
        (origin / "bot/main.py").write_text("pass\n")
        (origin / ".env").write_text("TOKEN=synthetic-secret")
        (origin / "bot/.env.private").write_text("TOKEN=synthetic-secret")
        (origin / "bot/link").symlink_to(self.base / "outside")
        (origin / "bot/node_modules").mkdir()
        (origin / "compose.yaml").write_text("services: {}\n")
        self.root.mkdir()
        self.subject(source_dir=origin).stage_source()
        self.assertTrue((self.root / "source/bot/main.py").exists())
        self.assertFalse((self.root / "source/.env").exists())
        self.assertFalse((self.root / "source/bot/.env.private").exists())
        self.assertFalse((self.root / "source/bot/link").is_symlink())
        self.assertFalse((self.root / "source/bot/node_modules").exists())

    def test_configuration_keeps_secrets_outside_source_and_is_stable(self):
        self.fixture_source()
        subject = self.subject()
        subject.stage_configuration()
        before = installer.read_env(subject.env_file)
        subject.stage_configuration()
        after = installer.read_env(subject.env_file)
        for key in ("SQ_DB_PASSWORD", "SQ_DB_ROOT_PASSWORD", "SQ_JWT_SECRET"):
            self.assertTrue(before[key])
            self.assertEqual(before[key], after[key])
        self.assertFalse((subject.source / ".env").exists())
        self.assertEqual(subject.descriptor["source_dir"], "source")
        self.assertEqual(subject.descriptor["env_file"], "config/private.env")
        self.assertEqual(subject.descriptor["extra_compose_files"], ["config/installer.compose.json"])
        self.assertNotIn(before["SQ_DB_PASSWORD"], subject.descriptor_file.read_text())
        self.assertTrue(all("--env-file" in command for command, _ in self.calls))

    def test_existing_secret_is_not_replaced_by_new_config(self):
        self.fixture_source()
        subject = self.subject()
        subject.stage_configuration()
        original = subject.env_file.read_bytes()
        config = self.base / "private.json"
        config.write_text('{"env":{"SQ_DB_PASSWORD":"different-synthetic"}}')
        with self.assertRaisesRegex(ValueError, "existing private credential"):
            self.subject(config=config).stage_configuration()
        self.assertEqual(subject.env_file.read_bytes(), original)

    def test_existing_managed_upgrade_overlay_survives_resume_configuration(self):
        self.fixture_source()
        subject = self.subject()
        subject.stage_configuration()
        installer.atomic_json(self.root / "config/images.compose.json", {"services": {"bot": {"image": "synthetic:approved"}}})
        subject.stage_configuration()
        self.assertIn("config/images.compose.json", subject.descriptor["extra_compose_files"])
        subject.compose("config", "--quiet")
        command = self.calls[-1][0]
        self.assertEqual(command.count(str(self.root / "config/images.compose.json")), 1)

    def test_preexisting_database_without_matching_secrets_is_refused(self):
        self.fixture_source()
        database = self.root / "data/sq-mysql"
        database.mkdir(parents=True)
        (database / "database.fixture").write_bytes(b"existing")
        with self.assertRaisesRegex(RuntimeError, "matching credentials"):
            self.subject().stage_configuration()

    def test_initial_restore_preserves_all_three_original_database_credentials(self):
        self.fixture_source()
        restored_env = self.root / "config/restored/main/private.env"
        credentials = {"SQ_DB_PASSWORD": "synthetic-db-original", "SQ_DB_ROOT_PASSWORD": "synthetic-root-original", "SQ_JWT_SECRET": "synthetic-jwt-original"}
        installer.write_env(restored_env, credentials)
        original = restored_env.read_bytes()
        for key in credentials:
            config = self.base / "private.json"
            config.write_text(json.dumps({"env": {key: "synthetic-different"}}))
            subject = self.subject(config=config)
            subject.state["restore"] = {"env_files": {"main": str(restored_env)}}
            with self.assertRaisesRegex(ValueError, "restored database credential"):
                subject.stage_configuration()
            self.assertEqual(restored_env.read_bytes(), original)
            self.assertFalse(subject.env_file.exists())

    def test_missing_credentials_never_starts_bot_even_if_wrapper_is_healthy(self):
        subject = self.subject()
        installer.write_env(subject.env_file, {"TELEGRAM_BOT_TOKEN": "", "SYNOLOGY_CHAT_ENABLED": "false"})
        subject.container_status = lambda: [{"Service": "wrapper", "Health": "healthy"}]
        subject.compose = mock.Mock()
        subject.stage_activation()
        subject.compose.assert_not_called()
        self.assertEqual(subject.state["business_status"], "needs_configuration")

    def test_token_does_not_bypass_wrapper_readiness(self):
        subject = self.subject()
        installer.write_env(subject.env_file, {"TELEGRAM_BOT_TOKEN": "synthetic-private"})
        subject.container_status = lambda: [{"Service": "wrapper", "Health": "starting"}]
        subject.compose = mock.Mock()
        subject.stage_activation()
        subject.compose.assert_not_called()
        self.assertEqual(subject.state["business_status"], "needs_configuration")

    def test_restored_token_is_held_until_current_config_explicitly_approves_cutover(self):
        self.fixture_source()
        subject = self.subject()
        subject.state["restored_needs_cutover"] = True
        subject.stage_configuration()
        values = installer.read_env(subject.env_file)
        values["TELEGRAM_BOT_TOKEN"] = "synthetic-old-token"
        values["START_BOT"] = "true"  # A bundle/env value is not approval.
        installer.write_env(subject.env_file, values)
        subject.stage_configuration()
        self.assertEqual(installer.read_env(subject.env_file)["START_BOT"], "false")
        subject.container_status = lambda: [{"Service": "wrapper", "Health": "healthy"}]
        subject.compose = mock.Mock()
        subject.stage_activation()
        subject.compose.assert_not_called()
        self.assertTrue(subject.state["restored_needs_cutover"])
        subject.config = {"START_BOT": "true"}
        subject.stage_configuration()
        self.assertFalse(subject.state["restored_needs_cutover"])
        self.assertIn("cutover_approved_at", subject.state)
        self.assertEqual(installer.read_env(subject.env_file)["START_BOT"], "true")

    def test_activation_starts_only_owned_services_after_configuration(self):
        subject = self.subject()
        installer.write_env(subject.env_file, {"TELEGRAM_BOT_TOKEN": "synthetic-private", "TELEGRAM_ALLOWED_CHAT_IDS": "local-fixture"})
        subject.container_status = lambda: [{"Service": "wrapper", "Health": "healthy"}]
        subject.compose = mock.Mock()
        subject.stage_activation()
        subject.compose.assert_called_once_with("up", "-d", "--no-deps", "bot", "wrapper-watchdog", "bot-watchdog", label="activate-bot-and-watchdogs")
        self.assertEqual(subject.state["business_status"], "configured")

    def test_create_stopped_containers_uses_compose_220_supported_flags(self):
        subject = self.subject()
        subject.compose = mock.Mock()
        subject.stage_containers()
        arguments = subject.compose.call_args.args
        self.assertEqual(arguments[:4], ("create", "--no-build", "--pull", "never"))
        self.assertNotIn("--no-deps", arguments)
        self.assertNotIn("start", arguments)
        self.assertNotIn("up", arguments)
        self.assertTrue(set(installer.RESTORE_TASK_WRITERS).issubset(arguments))

    def test_restored_pending_cutover_starts_infrastructure_but_no_task_writers(self):
        subject = self.subject(with_media_servers=True)
        subject.state.update({"restore": {"mounts": []}, "restored_needs_cutover": True})
        subject.compose = mock.Mock()
        subject.stage_services()
        arguments = subject.compose.call_args.args
        self.assertFalse(set(installer.RESTORE_TASK_WRITERS).intersection(arguments))
        self.assertTrue({"legacy-wrapper", "wrapper", "youtube-pot", "sq-mysql", "sq-neteasecloudmusicapi", "navidrome", "emby", "lrcapi"}.issubset(arguments))
        self.assertNotIn("restored_writers_released", subject.state)

    def test_pending_cutover_stops_all_existing_task_writers_even_with_ready_bot(self):
        subject = self.subject()
        subject.state["restored_needs_cutover"] = True
        installer.write_env(subject.env_file, {"START_BOT": "true", "TELEGRAM_BOT_TOKEN": "synthetic-private", "TELEGRAM_ALLOWED_CHAT_IDS": "fixture"})
        subject.container_status = lambda: [{"Service": name, "State": "running"} for name in installer.RESTORE_TASK_WRITERS] + [{"Service": "wrapper", "State": "running", "Health": "healthy"}]
        subject.compose = mock.Mock()
        subject.stage_activation()
        subject.compose.assert_called_once_with("stop", *installer.RESTORE_TASK_WRITERS, label="hold-restored-task-writers")
        self.assertEqual(subject.state["business_status"], "needs_configuration")

    def test_approved_restore_resume_releases_held_queues_with_empty_bot_token(self):
        subject = self.subject()
        subject.state.update({"restore": {"mounts": []}, "restored_needs_cutover": False,
                              "completed": ["services"]})
        installer.write_env(subject.env_file, {"START_BOT": "true", "TELEGRAM_BOT_TOKEN": ""})
        subject.container_status = lambda: [{"Service": "wrapper", "State": "running", "Health": "healthy"}]
        subject.compose = mock.Mock()
        subject.stage_activation()
        subject.compose.assert_called_once_with("up", "-d", "--no-deps", *installer.RESTORE_QUEUE_SERVICES,
                                                label="activate-restored-queue-services", timeout=600)
        self.assertTrue(subject.state["restored_writers_released"])
        self.assertEqual(subject.state["business_status"], "needs_configuration")
        subject.compose.reset_mock()
        subject.stage_activation()
        subject.compose.assert_not_called()

    def test_restore_verification_requires_infrastructure_running_and_writers_stopped(self):
        subject = self.subject()
        subject.state["restored_needs_cutover"] = True
        rows = [{"Service": name, "State": "running", "Health": "healthy"}
                for name in ("legacy-wrapper", "wrapper", "youtube-pot", "sq-mysql", "sq-neteasecloudmusicapi")]
        rows += [{"Service": name, "State": "created"} for name in installer.RESTORE_TASK_WRITERS]
        subject.container_status = lambda: rows
        subject.stage_verification()
        self.assertTrue(subject.state["infrastructure_ready"])
        rows[-1]["State"] = "running"
        with self.assertRaisesRegex(RuntimeError, "task writers must remain stopped"):
            subject.stage_verification()

    def test_installation_lock_refuses_concurrent_run(self):
        with installer.install_lock(self.root):
            with self.assertRaisesRegex(RuntimeError, "another installer"):
                with installer.install_lock(self.root):
                    self.fail("concurrent lock succeeded")

    def test_other_projects_container_is_never_managed(self):
        subject = self.subject()
        subject.descriptor = {"schema": 1}
        def run(command, label, **kwargs):
            output = "fixture-id\n" if command[1] == "ps" else json.dumps({installer.OWNER_LABEL: "someone-else"})
            return subprocess.CompletedProcess(command, 0, output, "")
        subject.run = run
        with self.assertRaisesRegex(RuntimeError, "another installation"):
            subject.check_existing_project()

    def test_compose_220_preflight_is_read_only(self):
        subject = self.subject()
        def run(command, **kwargs):
            text = "2.20.1\n" if command[1:3] == ["compose", "version"] else ""
            return subprocess.CompletedProcess(command, 0, text, "")
        subject.runner = run
        with mock.patch.object(installer.platform, "system", return_value="Linux"), \
                mock.patch.object(installer.platform, "machine", return_value="x86_64"), \
                mock.patch.object(installer.os, "access", return_value=True), \
                mock.patch.object(installer, "available_memory_gib", return_value=8), \
                mock.patch.object(installer.shutil, "disk_usage", return_value=type("Disk", (), {"free": 100 * 1024 ** 3})()), \
                mock.patch.object(installer.socket, "socket"):
            result = subject.preflight()
        self.assertEqual(result["compose"], "2.20.1")
        self.assertFalse(self.root.exists())

    def test_preflight_uses_existing_owned_runtime_for_memory_headroom(self):
        core = {"legacy-wrapper", "wrapper", "youtube-pot", "lxserver", "sq-mysql", "sq-neteasecloudmusicapi",
                "sqmusic-server", "word-lyrics-resolver", "word-lyrics-nas-aligner"}
        cases = [
            ("owned", 0.75, {}, core, True, False, "resume"),
            ("fresh", 0.75, {}, set(), False, False, None),
            ("descriptor-only", 0.75, {}, set(), True, False, None),
            ("missing-core", 0.75, {}, core - {"word-lyrics-resolver"}, True, False, None),
            ("more-vm-ram", 0.75, {"VM_MEMORY": "8192"}, core, True, False, None),
            ("unapplied-memory-change", 0.75, {}, core, True, False, None),
            ("new-profile", 0.75, {}, core | {"navidrome", "emby", "lrcapi"}, True, True, None),
            ("custom-resume-minimum", 0.75, {"INSTALL_MIN_RESUME_AVAILABLE_GIB": "1"}, core, True, False, None),
            ("explicit-fresh-override", 0.75, {"INSTALL_MIN_AVAILABLE_GIB": "0.5"}, set(), False, False, "full"),
            ("held-restore", 2.25, {}, core - set(installer.RESTORE_TASK_WRITERS), True, False, "restore"),
            ("held-restore-too-low", 0.75, {}, core - set(installer.RESTORE_TASK_WRITERS), True, False, None),
            ("held-restore-missing-db", 2.25, {}, core - set(installer.RESTORE_TASK_WRITERS) - {"sq-mysql"}, True, False, None),
        ]
        for name, available, config, running, described, media, expected in cases:
            with self.subTest(name=name):
                self.root = self.base / name
                subject = self.subject(with_media_servers=media)
                subject.config.update(config)
                installer.write_env(subject.env_file, {"VM_MEMORY": "8192" if name == "unapplied-memory-change" else "4096"})
                if described:
                    subject.descriptor = {"profiles": list(installer.PROFILES), "installation_id": subject.owner}
                if name.startswith("held-restore"):
                    subject.state.update({"restore": {"schema": 1}, "restored_needs_cutover": True})
                services = core | running if described else set()
                def run(command, **kwargs):
                    self.calls.append((command, kwargs))
                    if command[1:3] == ["compose", "version"]:
                        output = "2.20.1\n"
                    elif command[1] == "ps":
                        output = "\n".join(sorted(services))
                    elif command[1] == "inspect":
                        service = command[-1]
                        output = (json.dumps({installer.OWNER_LABEL: subject.owner, "com.docker.compose.service": service})
                                  if command[-2] == "{{json .Config.Labels}}" else json.dumps(service in running))
                    elif command[1] == "exec":
                        self.assertEqual(command[-2:], ["printenv", "VM_MEMORY"])
                        output = "4096\n"
                    else:
                        output = ""
                    return subprocess.CompletedProcess(command, 0, output, "")
                subject.runner = run
                with mock.patch.object(installer.platform, "system", return_value="Linux"), \
                        mock.patch.object(installer.platform, "machine", return_value="x86_64"), \
                        mock.patch.object(installer.os, "access", return_value=True), \
                        mock.patch.object(installer, "available_memory_gib", return_value=available), \
                        mock.patch.object(installer.shutil, "which", return_value="/usr/bin/git"), \
                        mock.patch.object(installer.shutil, "disk_usage", return_value=type("Disk", (), {"free": 100 * 1024 ** 3})()), \
                        mock.patch.object(installer.socket, "socket"):
                    if expected is None:
                        with self.assertRaisesRegex(RuntimeError, "insufficient available RAM"):
                            subject.preflight()
                    else:
                        result = subject.preflight()
                        self.assertEqual(result["memory_budget"], expected)
                        self.assertEqual(result["available_memory_gib"], available)
                        self.assertEqual(result["required_available_memory_gib"], 2 if expected == "restore" else 0.5)

    def test_low_ram_does_not_bypass_live_container_ownership_check(self):
        subject = self.subject()
        subject.descriptor = {"profiles": list(installer.PROFILES)}
        def run(command, **kwargs):
            if command[1:3] == ["compose", "version"]:
                output = "2.20.1"
            elif command[1] == "ps":
                output = "foreign-vm"
            elif command[1] == "inspect":
                output = json.dumps({installer.OWNER_LABEL: "another-installation", "com.docker.compose.service": "legacy-wrapper"})
            else:
                output = ""
            return subprocess.CompletedProcess(command, 0, output, "")
        subject.runner = run
        with mock.patch.object(installer.platform, "system", return_value="Linux"), \
                mock.patch.object(installer.platform, "machine", return_value="x86_64"), \
                mock.patch.object(installer.os, "access", return_value=True), \
                mock.patch.object(installer, "available_memory_gib", return_value=0.75), \
                mock.patch.object(installer.shutil, "which", return_value="/usr/bin/git"), \
                mock.patch.object(installer.shutil, "disk_usage", return_value=type("Disk", (), {"free": 100 * 1024 ** 3})()):
            with self.assertRaisesRegex(RuntimeError, "another installation"):
                subject.preflight()

    def test_token_without_allowlist_is_never_activated(self):
        subject = self.subject()
        installer.write_env(subject.env_file, {"TELEGRAM_BOT_TOKEN": "synthetic-private"})
        subject.container_status = lambda: [{"Service": "wrapper", "Health": "healthy"}]
        subject.compose = mock.Mock()
        subject.stage_activation()
        subject.compose.assert_not_called()
        self.assertEqual(subject.state["business_status"], "needs_configuration")
        self.assertTrue(any("allowlist" in item for item in subject.state["pending"]))

    def test_incomplete_enabled_synology_route_blocks_otherwise_valid_telegram(self):
        subject = self.subject()
        installer.write_env(subject.env_file, {"TELEGRAM_BOT_TOKEN": "synthetic-private", "TELEGRAM_ALLOWED_CHAT_IDS": "fixture",
            "SYNOLOGY_CHAT_ENABLED": "true", "SYNOLOGY_CHAT_OUTGOING_TOKEN": "synthetic-outgoing"})
        subject.container_status = lambda: [{"Service": "wrapper", "Health": "healthy"}]
        subject.compose = mock.Mock()
        subject.stage_activation()
        subject.compose.assert_not_called()

    def test_restore_images_are_verified_and_replace_only_owned_service_images(self):
        subject = self.subject()
        image = "sha256:" + "1" * 64
        subject.state["restore"] = {"image_archive": str(self.base / "verified-images.tar"),
            "images": [{"service": "bot", "image_id": image}, {"service": "unrelated", "image_id": "ignored"}]}
        subject.descriptor = {"owned_services": ["bot"]}
        overlay = self.root / "config/installer.compose.json"
        installer.atomic_json(overlay, {"services": {"bot": {"labels": {installer.OWNER_LABEL: subject.owner}}}})
        commands = []
        def run(command, label, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, image + "\n" if "inspect" in command else "", "")
        subject.run = run
        subject.stage_images()
        self.assertEqual(subject.state["restored_images"], {"bot": image})
        self.assertEqual(installer.read_json(overlay)["services"]["bot"]["image"], image)
        self.assertEqual(commands[0][:3], ["docker", "image", "load"])
        self.assertEqual(commands[1][-1], image)

    def test_restored_images_skip_core_build_pull_and_sq_source_fetch(self):
        from deploy import dependencies
        subject = self.subject()
        subject.state["restored_images"] = {name: "sha256:" + "1" * 64 for name in installer.SERVICES}
        installer.write_env(subject.env_file, {"STATE_PATH": str(self.root / "data/state"), "SQ_PATCHED_IMAGE": "unused:restored"})
        subject.compose = mock.Mock()
        with mock.patch.object(dependencies, "prepare_models") as models, \
                mock.patch.object(dependencies, "prepare_sq") as sq:
            subject.stage_dependencies()
        models.assert_called_once()
        sq.assert_not_called()
        subject.stage_build()
        subject.stage_pull()
        subject.compose.assert_not_called()

    def test_failed_core_service_does_not_count_as_successful_installation(self):
        subject = self.subject()
        subject.container_status = lambda: [{"Service": "wrapper", "State": "running", "Health": "healthy"}]
        with self.assertRaisesRegex(RuntimeError, "did not remain running"):
            subject.stage_verification()

    def test_custom_unprovisioned_model_path_is_not_reported_ready(self):
        self.fixture_source()
        config = self.base / "private.json"
        config.write_text('{"env":{"WORD_LYRICS_NAS_WHISPER_MODEL":"/unknown/model.bin"}}')
        with self.assertRaisesRegex(ValueError, "verified model path"):
            self.subject(config=config).stage_configuration()

    def test_navidrome_mount_roots_are_private_owned_and_do_not_touch_database_bytes(self):
        subject = self.subject(with_media_servers=True)
        state = self.root / "data/navidrome"
        state.mkdir(parents=True)
        database = state / "navidrome.db"
        database.write_bytes(b"synthetic existing database bytes")
        database.chmod(0o600)
        values = {"NAVIDROME_DATA_PATH": str(state), "PUID": "1234", "PGID": "2345"}
        with mock.patch.object(installer.os, "geteuid", return_value=0), mock.patch.object(installer.os, "chown") as chown:
            subject.prepare_navidrome_music_root(values)
        parent = state / "music-root"
        paths = [state, parent] + [parent / name for name in ("apple", "youtube", "bilibili")]
        self.assertEqual({path.name for path in parent.iterdir()}, {"apple", "youtube", "bilibili"})
        for path in paths:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertIn(mock.call(str(path), 1234, 2345), chown.call_args_list)
        self.assertTrue(all(not list((parent / name).iterdir()) for name in ("apple", "youtube", "bilibili")))
        self.assertEqual(database.read_bytes(), b"synthetic existing database bytes")
        self.assertEqual(database.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(mock.call(str(database), 1234, 2345), chown.call_args_list)

    def test_navidrome_mount_points_refuse_symlinks_without_touching_external_data(self):
        subject = self.subject(with_media_servers=True)
        state = self.root / "data/navidrome"
        state.mkdir(parents=True)
        outside = self.base / "unrelated"
        outside.mkdir(mode=0o755)
        before = outside.stat().st_mode
        (state / "music-root").symlink_to(outside, target_is_directory=True)
        values = {"NAVIDROME_DATA_PATH": str(state), "PUID": str(os.getuid()), "PGID": str(os.getgid())}
        with self.assertRaisesRegex(RuntimeError, "must not be symlinks"):
            subject.prepare_navidrome_music_root(values)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(outside.stat().st_mode, before)

    def test_resume_prepares_navidrome_parent_before_containers_despite_credentials_checkpoint(self):
        self.fixture_source()
        marker = self.root / "existing-credential.fixture"
        marker.write_text("synthetic-original")
        installer.atomic_json(self.root / "state/install.json", {"completed": ["credentials"], "with_media_servers": True})
        calls = {"credentials": 0, "containers": 0}
        class ResumeFixture(installer.Installer):
            def preflight(self):
                return {}
            def stage_credentials(self):
                calls["credentials"] += 1
                raise AssertionError("completed credentials must not be regenerated")
            def stage_containers(self):
                calls["containers"] += 1
                parent = Path(installer.read_env(self.env_file)["NAVIDROME_DATA_PATH"]) / "music-root"
                if {path.name for path in parent.iterdir()} != {"apple", "youtube", "bilibili"}:
                    raise AssertionError("required mount points missing before container creation")
            def container_status(self):
                return []
        with mock.patch.object(installer, "STAGES", ["configuration", "credentials", "containers"]), redirect_stdout(io.StringIO()):
            first = ResumeFixture(self.root, runner=self.runner)
            first.install()
            values = installer.read_env(first.env_file)
            parent = Path(values["NAVIDROME_DATA_PATH"]) / "music-root"
            for child in parent.iterdir():
                child.rmdir()
            parent.rmdir()
            ResumeFixture(self.root, runner=self.runner).install()
        self.assertEqual(calls, {"credentials": 0, "containers": 1})
        for path in [parent] + list(parent.iterdir()):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_uid, int(values["PUID"]))
            self.assertEqual(path.stat().st_gid, int(values["PGID"]))
        self.assertEqual(marker.read_text(), "synthetic-original")

    def test_failure_and_resume_keep_completed_credentials_and_retry_failed_stage(self):
        calls = []
        fail = {"once": True}
        class Fixture(installer.Installer):
            def preflight(self):
                return {}
            def stage_configuration(self):
                self.descriptor = {"schema": 1}
                installer.atomic_json(self.descriptor_file, self.descriptor)
                calls.append("configuration")
            def stage_credentials(self):
                (self.root / "credential.fixture").write_text("synthetic-original")
                calls.append("credentials")
            def stage_build(self):
                calls.append("build")
                if fail["once"]:
                    fail["once"] = False
                    raise RuntimeError("synthetic failure")
            def stage_activation(self):
                self.state["business_status"] = "needs_configuration"
            def container_status(self):
                return []
        stages = ["configuration", "credentials", "build", "activation"]
        with mock.patch.object(installer, "STAGES", stages), redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                Fixture(self.root).install()
            state = installer.read_json(self.root / "state/install.json")
            self.assertEqual(state["failed_stage"], "build")
            self.assertIn("credentials", state["completed"])
            result = Fixture(self.root).install()
        self.assertTrue(result["installed"])
        self.assertFalse(result["ready"])
        self.assertEqual(calls.count("credentials"), 1)
        self.assertEqual(calls.count("build"), 2)
        self.assertEqual((self.root / "credential.fixture").read_text(), "synthetic-original")

    def test_configuration_resume_applies_vm_mode_and_new_media_without_rebuilding(self):
        source = self.fixture_source()
        with (source / ".env.example").open("a") as handle:
            handle.write("WRAPPER_SINGLE_ENABLE=false\nWRAPPER_DEVICE_INFO=\nVM_CPUS=2\n")
        runtime, completed = {}, {"dependencies": 0, "build": 0, "pull": 0}
        fail = {"services": False}

        class RuntimeFixture(installer.Installer):
            def preflight(self):
                return {}
            def stage_dependencies(self):
                completed["dependencies"] += 1
            def stage_build(self):
                completed["build"] += 1
            def stage_pull(self):
                completed["pull"] += 1
            def compose(self, *arguments, **options):
                if arguments[0] == "up":
                    if fail["services"]:
                        fail["services"] = False
                        raise RuntimeError("synthetic interrupted service apply")
                    values = installer.read_env(self.env_file)
                    for name in arguments:
                        if name in set(installer.SERVICES) | {"navidrome", "emby", "lrcapi"}:
                            runtime[name] = {"Service": name, "State": "running", "Health": "healthy"}
                            if name == "legacy-wrapper":
                                runtime[name]["single"] = values["WRAPPER_SINGLE_ENABLE"] == "true"
                                runtime[name]["device"] = values["WRAPPER_DEVICE_INFO"]
                return subprocess.CompletedProcess(arguments, 0, "")
            def container_status(self):
                return list(runtime.values())

        config = self.base / "private-config.json"
        config.write_text(json.dumps({"env": {"WRAPPER_SINGLE_ENABLE": True, "WRAPPER_DEVICE_INFO": "synthetic-device"}}))
        stages = ["configuration", "dependencies", "build", "pull", "services", "activation", "verification"]
        with mock.patch.object(installer, "STAGES", stages), redirect_stdout(io.StringIO()):
            RuntimeFixture(self.root).install()
            self.assertFalse(runtime["legacy-wrapper"]["single"])
            fail["services"] = True
            with self.assertRaisesRegex(RuntimeError, "interrupted service apply"):
                RuntimeFixture(self.root, config=config).install()
            self.assertFalse(runtime["legacy-wrapper"]["single"])
            RuntimeFixture(self.root, config=config).install()
            self.assertTrue(runtime["legacy-wrapper"]["single"])
            self.assertEqual(runtime["legacy-wrapper"]["device"], "synthetic-device")
            self.assertEqual(completed, {"dependencies": 1, "build": 1, "pull": 1})
            RuntimeFixture(self.root, config=config, with_media_servers=True).install()
        self.assertTrue(all(runtime[name]["State"] == "running" for name in ("navidrome", "emby", "lrcapi")))
        self.assertEqual(completed, {"dependencies": 1, "build": 1, "pull": 2})

    def test_active_bot_or_lyric_work_blocks_config_writes_but_queued_work_allows_resume(self):
        for status in ("running", "stopping", "leased_alignment", "queued"):
            with self.subTest(status=status):
                self.root = self.base / ("installation-" + status)
                self.fixture_source()
                subject = self.subject()
                subject.stage_configuration()
                subject.state["completed"] = ["configuration", "services"]
                subject.state["restored_needs_cutover"] = True
                before = {path: path.read_bytes() for path in (subject.env_file, subject.descriptor_file, self.root / "config/installer.compose.json")}
                values = installer.read_env(subject.env_file)
                state = Path(values["STATE_PATH"])
                lyric = status.startswith("leased_")
                path = state / ("word-lyrics/automation/queue.sqlite3" if lyric else "task-cards.sqlite3")
                path.parent.mkdir(parents=True, exist_ok=True)
                table = "jobs" if lyric else "task_cards"
                with sqlite3.connect(path) as database:
                    database.execute("CREATE TABLE " + table + " (status TEXT)")
                    database.execute("INSERT INTO " + table + " VALUES (?)", (status,))
                subject.config = {"WRAPPER_SINGLE_ENABLE": "true", "WRAPPER_DEVICE_INFO": "synthetic-device", "START_BOT": "true"}
                previous_commands = len(self.calls)
                if status != "queued":
                    with self.assertRaisesRegex(RuntimeError, "Active bot or lyric"):
                        subject.stage_configuration()
                    self.assertEqual({path: path.read_bytes() for path in before}, before)
                    self.assertEqual(len(self.calls), previous_commands)
                    self.assertTrue(subject.state["restored_needs_cutover"])
                    self.assertNotIn("cutover_approved_at", subject.state)
                    self.assertIn("services", subject.state["completed"])
                else:
                    subject.stage_configuration()
                    self.assertEqual(installer.read_env(subject.env_file)["WRAPPER_SINGLE_ENABLE"], "true")
                    self.assertFalse(subject.state["restored_needs_cutover"])
                    self.assertNotIn("services", subject.state["completed"])
                with sqlite3.connect(path) as database:
                    self.assertEqual(database.execute("SELECT status FROM " + table).fetchone()[0], status)


if __name__ == "__main__":
    unittest.main()
