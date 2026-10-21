"""Pure local harness guards; the actual acceptance workflow uses real Docker."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import sys

from deploy import acceptance


class AcceptanceGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()

    def test_existing_nonempty_workspace_is_refused_before_docker(self):
        root = self.base / "occupied"
        root.mkdir()
        (root / "keep").write_text("existing")
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.Harness(root, self.base / "report.json")
        self.assertEqual((root / "keep").read_text(), "existing")

    def test_report_cannot_pollute_strict_private_bundle(self):
        root = self.base / "new"
        with self.assertRaises(acceptance.AcceptanceError):
            acceptance.Harness(root, root / "bundle/report.json")
        self.assertFalse(root.exists())

    def test_source_schema_creates_real_task_table_with_queued_default(self):
        with sqlite3.connect(":memory:") as database:
            database.executescript(acceptance.source_task_schema(acceptance.SOURCE))
            database.execute("INSERT INTO task_cards(job_key,job_json,source,chat_id,kind,mode,title,created_at) VALUES(?,?,?,?,?,?,?,?)",
                             ("synthetic", "{}", "acceptance", "local", "acceptance_fixture", "none", "fixture", 0))
            self.assertEqual(database.execute("SELECT id,status FROM task_cards").fetchone(), (1, "queued"))

    def test_clean_environment_drops_personal_tokens_without_mutating_caller(self):
        original = {"PATH": "/bin", "TELEGRAM_BOT_TOKEN": "synthetic-private", "SQ_DB_PASSWORD": "synthetic-db",
                    "YT_DOWNLOAD_PROXY_URL": "http://synthetic-user:synthetic-pass@example.test", "DOCKER_HOST": "unix:///var/run/docker.sock"}
        clean = acceptance.clean_environment_from(original, acceptance.SOURCE)
        self.assertEqual(clean, {"PATH": "/bin", "DOCKER_HOST": "unix:///var/run/docker.sock"})
        self.assertIn("TELEGRAM_BOT_TOKEN", original)

    def test_initial_report_has_no_private_paths_or_claimed_business_acceptance(self):
        harness = acceptance.Harness(self.base / "new", self.base / "report.json")
        text = json.dumps(harness.result)
        self.assertNotIn(str(self.base), text)
        self.assertFalse(harness.result["passed"])
        self.assertIn("real music/video download and highest formats", harness.result["not_tested"])

    def test_safe_build_diagnostic_masks_secrets_userinfo_queries_and_local_paths(self):
        text = "#8 ERROR at /local/workspace/source: https://user:pass@public.example/file?signature=private-query synthetic-db-password"
        clean = acceptance.redact_diagnostic(text, ["synthetic-db-password"], Path("/local/workspace"), Path("/public/source"))
        for value in ("user:pass", "private-query", "synthetic-db-password", "/local/workspace"):
            self.assertNotIn(value, clean)
        self.assertIn("public.example/file", clean)
        self.assertIn("ERROR", clean)

    def test_only_real_credentials_and_proxy_urls_are_redacted(self):
        values = {"YT_AUTH_RETRY_DELAY_SECONDS": "2", "YT_PO_TOKEN_ENABLED": "true",
                  "ONLINE_AUTH_SUCCESS_COOLDOWN_SECONDS": "300", "YT_AUTH_AUTO_REFRESH_DISABLED": "false",
                  "AUTH_BROWSER_IMAGE": "public-image:2026", "YT_COOKIES_PATH": "/state/cookies.txt",
                  "WRAPPER_MANAGER_PROXY": "http://proxy-user:proxy-pass@proxy.example:8888",
                  "SQ_DB_PASSWORD": "synthetic-db", "TELEGRAM_BOT_TOKEN": "synthetic-token"}
        private = acceptance.credential_values(values)
        self.assertEqual(set(private), {values["WRAPPER_MANAGER_PROXY"], "synthetic-db", "synthetic-token"})
        clean = acceptance.redact_diagnostic("ERROR stage 2 sha256 2026 true false synthetic-db", private, self.base, acceptance.SOURCE)
        self.assertIn("stage 2 sha256 2026 true false", clean)
        self.assertNotIn("synthetic-db", clean)

    def test_failed_stage_diagnostics_take_priority_over_successful_build_output(self):
        harness = acceptance.Harness(self.base / "new", self.base / "report.json")
        acceptance.atomic_json(harness.original / "state/install.json", {"failed_stage": "services"})
        from deploy.installer import write_env
        write_env(harness.original / "config/private.env", {"SQ_DB_PASSWORD": "synth-db", "YT_AUTH_RETRY_DELAY_SECONDS": "2"})
        logs = harness.original / "state/logs"
        logs.mkdir()
        (logs / "start-core-services.log").write_text("dependency failed to start: wrapper unhealthy, stage 2 synth-db\nCookie: hidden-payload ERROR\n")
        (logs / "sq-dependency-build.log").write_text("#9 DONE 0.1s\n" * 100 + "ERROR unrelated-successful-build\n")
        (logs / "container-application.log").write_text("ERROR private-application-payload\n")
        harness.safe_diagnostics()
        diagnostics = "\n".join(harness.result["safe_build_diagnostics"])
        self.assertIn("services:start-core-services", diagnostics)
        self.assertIn("wrapper unhealthy, stage 2", diagnostics)
        for value in ("synth-db", "hidden-payload", "unrelated-successful-build", "private-application-payload"):
            self.assertNotIn(value, diagnostics)

    def test_sq_initialization_failure_selects_its_own_log(self):
        harness = acceptance.Harness(self.base / "new", self.base / "report.json")
        acceptance.atomic_json(harness.restored / "state/install.json", {"failed_stage": "sq_configuration"})
        logs = harness.restored / "state/logs"
        logs.mkdir()
        (logs / "sq-schema-configure.log").write_text("ERROR schema was not initialized\n")
        harness.safe_diagnostics()
        self.assertIn("sq_configuration:sq-schema-configure", harness.result["safe_build_diagnostics"][0])

    def test_install_progress_never_prints_arbitrary_private_state(self):
        harness = acceptance.Harness(self.base / "new", self.base / "report.json")
        acceptance.atomic_json(harness.original / "state/install.json", {
            "completed": acceptance.STAGES[:8], "failed_stage": "synthetic-secret-field", "private": "synthetic-secret-field"})
        self.assertEqual(acceptance.installer_progress(harness.original), acceptance.STAGES[8])
        output = io.StringIO()
        with redirect_stdout(output):
            harness.command([sys.executable, "-c", "print('synthetic-private-command-output')"], "progress-fixture", progress_root=harness.original)
        self.assertIn("Installer progress: original " + acceptance.STAGES[8], output.getvalue())
        self.assertNotIn("synthetic", output.getvalue())

    def test_cli_prints_only_sanitized_report_object_after_run(self):
        report = {"passed": False, "stage": "fresh_install", "failure": "original_install_exit_1"}
        output = io.StringIO()
        with mock.patch.object(acceptance.Harness, "run", return_value=report), redirect_stdout(output):
            result = acceptance.main(["--root", str(self.base / "new"), "--report", str(self.base / "report.json")])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1]), report)

    def test_failure_category_never_serializes_arbitrary_exception_text(self):
        self.assertEqual(acceptance.safe_failure(RuntimeError("Cookie: synthetic-secret")), "unexpected_runtimeerror")
        self.assertEqual(acceptance.safe_failure(acceptance.AcceptanceError("sq_initialization_not_applied")), "sq_initialization_not_applied")

    def test_restore_hold_requires_every_writer_created_and_never_started(self):
        harness = acceptance.Harness(self.base / "new", self.base / "report.json")
        rows = [{"service": name, "running": False, "started_at": "0001-01-01T00:00:00Z"}
                for name in acceptance.RESTORE_TASK_WRITERS]
        harness.assert_restored_writers_held(rows)
        for state in ({"running": True}, {"started_at": "2026-10-21T00:00:00Z"}):
            changed = [dict(row) for row in rows]
            changed[1].update(state)
            with self.assertRaisesRegex(acceptance.AcceptanceError, "before_cutover"):
                harness.assert_restored_writers_held(changed)
        with self.assertRaises(acceptance.AcceptanceError):
            harness.assert_restored_writers_held(rows[:-1])

    def test_explicit_cutover_keeps_bot_token_empty_and_uses_resume(self):
        harness = acceptance.Harness(self.base / "new", self.base / "report.json")
        acceptance.atomic_json(harness.root / "restored-private-config.json", {"env": {"START_BOT": "false", "TELEGRAM_BOT_TOKEN": ""}})
        calls = []
        harness.command = lambda command, *args, **kwargs: calls.append(command)
        harness.approve_restored_cutover()
        values = acceptance.read_json(harness.root / "restored-private-config.json")["env"]
        self.assertEqual(values["START_BOT"], "true")
        self.assertEqual(values["TELEGRAM_BOT_TOKEN"], "")
        self.assertIn("resume", calls[0])


if __name__ == "__main__":
    unittest.main()
