"""Pure local harness guards; the actual acceptance workflow uses real Docker."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

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
