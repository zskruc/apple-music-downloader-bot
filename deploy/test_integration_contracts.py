"""Contracts spanning the real Compose templates, installer metadata and migration."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

from deploy import installer, migration

SOURCE = Path(__file__).resolve().parents[1]


class SharedMusicMigrationContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "installed"
        self.source = self.root / "source"
        self.source.mkdir(parents=True)
        shutil.copyfile(SOURCE / ".env.example", self.source / ".env.example")
        files = ["compose.yaml", "services/lx-music/compose.yaml", "services/sq-music/compose.yaml", "compose.self-heal.yaml"]
        for filename in files:
            target = self.source / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SOURCE / filename, target)
        subject = installer.Installer(self.root, runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, ""))
        subject.stage_configuration()
        self.values = installer.read_env(subject.env_file)
        self.config = {"services": {"bot": {"volumes": []}, "lxserver": {"volumes": []}}}
        bindings = {service: {} for service in self.config["services"]}
        expression = re.compile(r"\$\{([A-Z0-9_]+)(?:(:-|:\?|\?|\-)([^}]*))?\}")
        def render(value):
            def substitute(match):
                key, operator, fallback = match.groups()
                if self.values.get(key):
                    return self.values[key]
                if operator in {"?", ":?"}:
                    self.fail("fixture environment is missing a required Compose variable: " + key)
                return fallback or ""
            return expression.sub(substitute, value)
        for filename in files:
            document = yaml.safe_load((self.source / filename).read_text())
            for service in bindings:
                for item in document.get("services", {}).get(service, {}).get("volumes", []):
                    if isinstance(item, str):
                        fields = render(item).split(":")
                        mount = {"type": "bind", "source": fields[0], "target": fields[1],
                                 "read_only": len(fields) > 2 and fields[2] == "ro"}
                    else:
                        mount = deepcopy(item)
                        mount["source"] = render(mount["source"])
                    bindings[service][mount["target"]] = mount
        for service, volumes in bindings.items():
            self.config["services"][service]["volumes"] = list(volumes.values())
            for mount in volumes.values():
                if mount["target"] == "/var/run/docker.sock":
                    continue
                source = Path(mount["source"])
                if mount["target"].startswith("/run/secrets/"):
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_text("synthetic matching authentication fixture")
                else:
                    source.mkdir(parents=True, exist_ok=True)
        self.music = Path(self.values["MUSIC_PATH"])
        self.lx_music = self.music / "LX Music"
        (self.lx_music / "Artist").mkdir()
        (self.lx_music / "Artist/track.flac").write_bytes(b"synthetic one-copy music fixture")
        self.rows = [{"id": "fixture-" + service, "project": subject.project, "service": service,
                      "running": False, "image_id": "sha256:" + "1" * 64,
                      "image_ref": "fixture@sha256:" + "1" * 64, "mounts": []}
                     for service in self.config["services"]]

    def capture(self, include_media):
        bundle = self.base / "bundle"
        with mock.patch.object(migration, "_compose_config", return_value=self.config), \
                mock.patch.object(migration, "_containers", return_value=self.rows):
            manifest = migration.snapshot(self.root, bundle, include_media=include_media)
        result = migration.restore_bundle(bundle, self.base / "new-host")
        return manifest, result

    def test_real_bot_and_lx_mounts_restore_one_shared_media_tree(self):
        manifest, result = self.capture(include_media=True)
        bot = next(item for item in result["mounts"] if item["service"] == "bot" and item["target"] == "/music")
        lx = next(item for item in result["mounts"] if item["service"] == "lxserver" and item["target"] == "/server/music/_open")
        self.assertEqual(bot["source_id"], lx["source_id"])
        self.assertEqual(lx["source_relative"], "LX Music")
        self.assertEqual(Path(lx["restored_path"]), Path(bot["restored_path"]) / "LX Music")
        track = Path(lx["restored_path"]) / "Artist/track.flac"
        self.assertEqual(track.read_bytes(), b"synthetic one-copy music fixture")
        self.assertTrue(track.samefile(Path(bot["restored_path"]) / "LX Music/Artist/track.flac"))
        self.assertEqual(sum(item["path"].endswith("/track.flac") for item in manifest["files"]), 1)

    def test_omitted_real_media_mounts_share_an_empty_tree_and_explicit_coverage(self):
        manifest, result = self.capture(include_media=False)
        bot = next(item for item in result["mounts"] if item["service"] == "bot" and item["target"] == "/music")
        lx = next(item for item in result["mounts"] if item["service"] == "lxserver" and item["target"] == "/server/music/_open")
        self.assertFalse(bot["included"])
        self.assertFalse(lx["included"])
        self.assertEqual(Path(lx["restored_path"]), Path(bot["restored_path"]) / "LX Music")
        self.assertTrue(Path(lx["restored_path"]).is_dir())
        self.assertFalse((Path(lx["restored_path"]) / "Artist/track.flac").exists())
        self.assertIn(lx["source_id"], {item["id"] for item in result["omitted_sources"]})
        self.assertIn(lx["source"], {item["source"] for item in manifest["coverage"]["omitted_mounts"] if "source" in item})


class RestoredDeploymentCutoverContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "new-host"
        self.subject = installer.Installer(self.root, runner=mock.Mock())

    def test_restored_background_queues_are_not_started_before_cutover(self):
        self.subject.state["restore"] = {"queue_policy": "offline byte-preserving snapshot"}
        self.subject.state["restored_needs_cutover"] = True
        self.subject.compose = mock.Mock()
        self.subject.stage_services()
        started = {argument for call in self.subject.compose.call_args_list for argument in call.args}
        self.assertFalse(started.intersection({"word-lyrics-resolver", "word-lyrics-nas-aligner"}),
                         "Holding only the bot still lets copied lyric queues run before cutover")


if __name__ == "__main__":
    unittest.main()
