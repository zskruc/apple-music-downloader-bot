"""Offline safety contracts for public SQ deployment helpers.

No upstream source fixture, real credentials, Docker calls, or network access.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def load_helper(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load_helper("sq_public_bootstrap", "bootstrap.py")
bridge = load_helper("sq_public_bridge", "prepare_build.py")


class PrivateBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sq-helper-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.staging = self.root / "staging"
        self.database = self.root / "database"
        self.auth = self.root / "secrets" / "sq-auth.json"
        self.env_file = self.root / ".env"
        self.env_file.write_text("\n".join([
            "OTHER_SETTING=preserve-this", f"SQ_STAGING_PATH={self.staging}",
            f"SQ_MYSQL_DATA_PATH={self.database}", f"SQ_AUTH_PATH={self.auth}",
            "SQ_DB_PASSWORD=", "SQ_DB_ROOT_PASSWORD=GENERATE_LOCALLY",
            "SQ_JWT_SECRET=REPLACE_WITH_PRIVATE_SECRET", "",
        ]), encoding="utf-8")

    def prepare(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bootstrap.prepare(self.env_file)
        return output.getvalue()

    def test_prepare_generates_independent_private_secrets_without_printing_them(self):
        output = self.prepare()
        _, values = bootstrap.read_env(self.env_file)
        auth = json.loads(self.auth.read_text())
        secrets = [auth["password"], values["SQ_DB_PASSWORD"],
                   values["SQ_DB_ROOT_PASSWORD"], values["SQ_JWT_SECRET"]]
        self.assertEqual(len(set(secrets)), len(secrets))
        self.assertGreaterEqual(len(values["SQ_JWT_SECRET"]), 64)
        for secret in secrets:
            self.assertNotIn(secret, output)
        self.assertEqual(os.stat(self.auth).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.env_file).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.auth.parent).st_mode & 0o777, 0o700)
        self.assertTrue(self.staging.is_dir())
        self.assertTrue(self.database.is_dir())
        self.assertIn("OTHER_SETTING=preserve-this", self.env_file.read_text())

    def test_prepare_preserves_credentials_even_after_database_initialization(self):
        self.prepare()
        before_env, before_auth = self.env_file.read_bytes(), self.auth.read_bytes()
        (self.database / "existing-database-fixture").write_text("fixture")
        self.prepare()
        self.assertEqual(self.env_file.read_bytes(), before_env)
        self.assertEqual(self.auth.read_bytes(), before_auth)

    def test_existing_database_without_matching_secrets_is_not_rekeyed(self):
        self.database.mkdir()
        (self.database / "existing-database-fixture").write_text("fixture")
        original = self.env_file.read_bytes()
        with self.assertRaisesRegex(SystemExit, "restore its matching secrets"):
            self.prepare()
        self.assertFalse(self.auth.exists())
        self.assertEqual(self.env_file.read_bytes(), original)

    def test_existing_database_with_missing_auth_is_not_rekeyed(self):
        self.prepare()
        (self.database / "existing-database-fixture").write_text("fixture")
        self.auth.unlink()
        with self.assertRaisesRegex(SystemExit, "restore its matching secrets"):
            self.prepare()
        self.assertFalse(self.auth.exists())

    def test_symlink_staging_is_rejected_before_creating_credentials(self):
        actual = self.root / "actual-staging"
        actual.mkdir()
        self.staging.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(SystemExit, "must not use a symlink"):
            self.prepare()
        self.assertFalse(self.auth.exists())

    def test_root_or_nested_private_directories_are_rejected_before_writes(self):
        original = self.env_file.read_text()
        replacements = [
            (f"SQ_STAGING_PATH={self.staging}", "SQ_STAGING_PATH=/"),
            (f"SQ_AUTH_PATH={self.auth}", "SQ_AUTH_PATH=/sq-auth.json"),
            (f"SQ_STAGING_PATH={self.staging}", f"SQ_STAGING_PATH={self.database / 'staging'}"),
        ]
        for before, after in replacements:
            with self.subTest(setting=before.split('=')[0]):
                self.env_file.write_text(original.replace(before, after))
                with self.assertRaises(SystemExit):
                    self.prepare()
                self.assertFalse(self.auth.exists())
                self.assertFalse(self.database.exists())

    def test_public_library_cannot_contain_sq_staging_or_private_state(self):
        self.env_file.write_text(self.env_file.read_text() + f"MUSIC_PATH={self.root}\n")
        with self.assertRaisesRegex(SystemExit, 'public MUSIC_PATH'):
            self.prepare()
        self.assertFalse(self.auth.exists())
        self.assertFalse(self.database.exists())

    def test_configure_requires_explicit_new_database_scope(self):
        with patch.object(bootstrap.subprocess, "run") as run:
            with self.assertRaisesRegex(SystemExit, "--new-database"):
                bootstrap.configure(self.env_file, confirmed_new_database=False)
        run.assert_not_called()

    def test_configure_checks_schema_and_supplies_credentials_only_via_stdin(self):
        self.prepare()
        auth = json.loads(self.auth.read_text())
        output = io.StringIO()
        success = SimpleNamespace(returncode=0, stdout=b"COUNT(DISTINCT config_key)\n9\n")
        with patch.object(bootstrap.subprocess, "run", return_value=success) as run:
            with contextlib.redirect_stdout(output):
                bootstrap.configure(self.env_file, confirmed_new_database=True)
        self.assertEqual(run.call_count, 2)
        preflight, update = run.call_args_list
        self.assertIn(b"SELECT COUNT(DISTINCT config_key)", preflight.kwargs["input"])
        self.assertNotIn(b"UPDATE sq_config", preflight.kwargs["input"])
        self.assertIn("sq-mysql", update.args[0])
        self.assertIn("--profile", update.args[0])
        self.assertIn(bootstrap.sql_string(auth["password"]).encode(), update.kwargs["input"])
        self.assertIn(b"http://sq-neteasecloudmusicapi:3000", update.kwargs["input"])
        self.assertNotIn(auth["password"], " ".join(update.args[0]))
        self.assertNotIn(auth["password"], output.getvalue())

    def test_unknown_schema_is_rejected_without_configuration_writes(self):
        self.prepare()
        unknown = SimpleNamespace(returncode=0, stdout=b"COUNT(DISTINCT config_key)\n8\n")
        with patch.object(bootstrap.subprocess, "run", return_value=unknown) as run:
            with self.assertRaisesRegex(SystemExit, "no changes applied"):
                bootstrap.configure(self.env_file, confirmed_new_database=True)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn(b"UPDATE sq_config", run.call_args.kwargs["input"])


class SourcePinTests(unittest.TestCase):
    def test_unknown_upstream_file_is_rejected_without_generating_overrides(self):
        with tempfile.TemporaryDirectory(prefix="sq-source-refusal-") as temporary:
            root = Path(temporary)
            for relative in bridge.FILES:
                fixture = root / "src/main/java" / relative
                fixture.parent.mkdir(parents=True, exist_ok=True)
                fixture.write_text("// original test fixture, deliberately not upstream\n")
            output = root / "generated"
            with self.assertRaisesRegex(SystemExit, "checksum differs"):
                bridge.prepare(root, output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
