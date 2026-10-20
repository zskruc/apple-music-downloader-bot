"""Deployment contracts for opt-in music services, without Docker or private files.

These tests inspect the shipped templates together: each service must reach the
bot, map the same actual files, and keep SQ's unfinished downloads private.
Only public example values and synthetic operator-supplied settings are used.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path, PurePosixPath
import re
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[2]
OVERRIDES = {"lx": "services/lx-music/compose.yaml", "sq": "services/sq-music/compose.yaml"}
SERVICE_NAMES = {"lx": "lxserver", "sq": "sqmusic-server"}


def example_environment() -> dict[str, str]:
    values = {}
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value.strip().strip("\"'")
    return values


def expand(value: str, environment: dict[str, str]) -> str:
    """Resolve only variable forms used by the public bind/env templates."""
    def substitute(match: re.Match[str]) -> str:
        name, operator, fallback = match.groups()
        setting = environment.get(name, "")
        if setting:
            return setting
        if operator == ":?":
            raise AssertionError(f"Test configuration is missing {name}")
        return fallback if operator == ":-" else ""
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|:\?)([^}]*))?\}", substitute, str(value))


def mounts(service: dict, environment: dict[str, str]) -> dict[str, dict]:
    result = {}
    for volume in service.get("volumes", []):
        if isinstance(volume, str):
            source, target, *options = expand(volume, environment).split(":")
            result[target] = {"source": source, "target": target,
                              "read_only": "ro" in options}
        else:
            item = dict(volume)
            item["source"] = expand(item["source"], environment)
            result[item["target"]] = item
    return result


def networks(service: dict) -> set[str]:
    return set(service.get("networks") or ["default"])


class OptionalMusicConfiguration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = yaml.safe_load((ROOT / "compose.yaml").read_text())
        cls.optional = {name: yaml.safe_load((ROOT / filename).read_text())
                        for name, filename in OVERRIDES.items()}
        cls.sample = example_environment()
        cls.environment = dict(cls.sample)
        # Simulate explicit provisioning without opening an operator's files.
        cls.environment.update({
            "LX_FRONTEND_AUTH_PATH": "/srv/private/lx-frontend-auth",
            "SQ_AUTH_PATH": "/srv/private/sq-auth.json",
            "SQ_STAGING_PATH": "/srv/private/sq-staging",
            "SQ_DB_PASSWORD": "fixture-db-password",
            "SQ_DB_ROOT_PASSWORD": "fixture-root-password",
            "SQ_JWT_SECRET": "fixture-jwt-secret-" * 4,
            "SQ_PATCHED_IMAGE": "fixture/sq-bridge:audited",
            "SQ_MYSQL_IMAGE": "fixture/mysql:audited",
            "SQ_NETEASE_IMAGE": "fixture/netease:audited",
        })

    def bot_environment(self, name: str) -> dict[str, str]:
        result = dict(self.environment)
        result.update({key: expand(value, self.environment)
                       for key, value in self.base["services"]["bot"].get("environment", {}).items()})
        result.update({key: expand(value, self.environment)
                       for key, value in self.optional[name]["services"]["bot"]["environment"].items()})
        return result

    def test_optional_features_default_off_in_example_and_application(self):
        names = {"UNIFIED_MUSIC_ENABLED", "LX_FALLBACK_ENABLED", "SQ_MUSIC_ENABLED"}
        tree = ast.parse((ROOT / "bot/ingest_bot.py").read_text())
        selected = [node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "env_flag"
                    or isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)]
        namespace = {"os": os}
        with patch.dict(os.environ, {}, clear=True):
            exec(compile(ast.Module(body=selected, type_ignores=[]), "<optional-config>", "exec"), namespace)
        for name in names:
            with self.subTest(feature=name):
                self.assertIs(namespace[name], False)
                self.assertEqual(self.sample["AMDL_" + name], "false")

    def test_database_admin_secrets_are_not_inherited_by_bot_or_lyric_workers(self):
        keys = ("SQ_DB_PASSWORD", "SQ_DB_ROOT_PASSWORD", "SQ_JWT_SECRET")
        for name in OVERRIDES:
            settings = self.bot_environment(name)
            for key in keys:
                self.assertEqual(settings[key], "")
        for name in ("word-lyrics-resolver", "word-lyrics-nas-aligner"):
            settings = self.base["services"][name]["environment"]
            for key in keys:
                self.assertEqual(settings[key], "")

    def test_default_stack_needs_no_optional_services_or_credential_files(self):
        bot = self.base["services"]["bot"]
        self.assertFalse(set(SERVICE_NAMES.values()) & set(self.base["services"]))
        self.assertFalse(set(SERVICE_NAMES.values()) & set(bot.get("depends_on", {})))
        self.assertFalse(any(target.startswith("/run/secrets/")
                             for target in mounts(bot, self.environment)))

    def test_service_urls_resolve_on_a_network_shared_with_bot(self):
        for name, target in SERVICE_NAMES.items():
            with self.subTest(service=name):
                service = self.optional[name]["services"][target]
                bot = dict(self.base["services"]["bot"])
                bot.update(self.optional[name]["services"]["bot"])
                common = networks(bot) & networks(service)
                self.assertTrue(common, "Bot and provider cannot communicate without a shared network")
                environment = self.bot_environment(name)
                url_key = "AMDL_LX_FALLBACK_BASE_URL" if name == "lx" else "AMDL_SQ_MUSIC_BASE_URL"
                hostname = urlsplit(environment[url_key]).hostname
                names = {target}
                network_settings = service.get("networks") or {}
                for network, settings in (network_settings.items() if isinstance(network_settings, dict) else []):
                    if network in common and isinstance(settings, dict):
                        names.update(settings.get("aliases", []))
                self.assertIn(hostname, names)
                self.assertEqual(service["profiles"], [name])

    def test_bot_credentials_are_read_only_existing_files(self):
        for name, key in (("lx", "AMDL_LX_FALLBACK_AUTH_FILE"), ("sq", "AMDL_SQ_AUTH_FILE")):
            with self.subTest(service=name):
                bot = self.optional[name]["services"]["bot"]
                credential = mounts(bot, self.environment)[self.bot_environment(name)[key]]
                self.assertEqual(credential.get("type"), "bind")
                self.assertTrue(credential.get("read_only"))
                self.assertIs(credential["bind"]["create_host_path"], False)
                self.assertTrue(Path(credential["source"]).is_absolute())

    def test_lx_and_bot_share_the_same_permanent_music_directory(self):
        environment = self.bot_environment("lx")
        service = self.optional["lx"]["services"][SERVICE_NAMES["lx"]]
        provider_mounts = mounts(service, self.environment)
        server_root = environment["AMDL_LX_FALLBACK_SERVER_MUSIC_ROOT"]
        library = Path(mounts(self.base["services"]["bot"], self.environment)["/music"]["source"])
        relative = PurePosixPath(environment["AMDL_LX_FALLBACK_OUTPUT_PATH"]).relative_to("/music")
        self.assertEqual(Path(provider_mounts[server_root]["source"]), library.joinpath(*relative.parts))
        self.assertFalse(provider_mounts[server_root].get("read_only", False))

    def test_sq_staging_is_shared_read_only_and_outside_the_public_library(self):
        environment = self.bot_environment("sq")
        service = self.optional["sq"]["services"][SERVICE_NAMES["sq"]]
        provider = mounts(service, self.environment)[environment["AMDL_SQ_MUSIC_SERVER_ROOT"]]
        bot = mounts(self.optional["sq"]["services"]["bot"], self.environment)[environment["AMDL_SQ_MUSIC_STAGING_PATH"]]
        self.assertEqual(Path(provider["source"]), Path(bot["source"]))
        self.assertTrue(bot.get("read_only"))
        self.assertFalse(provider.get("read_only", False))
        library = Path(mounts(self.base["services"]["bot"], self.environment)["/music"]["source"])
        self.assertFalse(Path(bot["source"]).is_relative_to(library))
        self.assertTrue(PurePosixPath(environment["AMDL_SQ_MUSIC_OUTPUT_PATH"]).is_relative_to("/music"))
        self.assertFalse(PurePosixPath(environment["AMDL_SQ_MUSIC_OUTPUT_PATH"]).is_relative_to(
            environment["AMDL_SQ_MUSIC_STAGING_PATH"]))

    def test_provider_admin_ports_bind_only_to_loopback(self):
        for name, template in self.optional.items():
            for service_name, service in template["services"].items():
                for port in service.get("ports", []):
                    with self.subTest(provider=name, service=service_name):
                        if isinstance(port, str):
                            self.assertTrue(expand(port, self.environment).startswith("127.0.0.1:"))
                        else:
                            self.assertIn(port.get("host_ip"), {"127.0.0.1", "::1"})


if __name__ == "__main__":
    unittest.main()
