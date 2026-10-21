from __future__ import annotations

import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.error

from deploy import dependencies as deps


DATA = b"synthetic model bytes for resumable deployment checks"


def model(name="fixture.bin", data=DATA):
    return deps.Model(name, hashlib.sha1(data).hexdigest(), hashlib.sha256(data).hexdigest(), len(data))


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


class Interrupted(Response):
    def __init__(self):
        super().__init__(b"")
        self.calls = 0

    def read(self, size=-1):
        self.calls += 1
        if self.calls == 1:
            return DATA[:11]
        raise OSError("synthetic network interruption")


class ModelPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.spec = model()

    def prepare(self, opener, attempts=1):
        with mock.patch.dict(deps.MODELS, {"primary": self.spec}, clear=True):
            return deps.prepare_models(self.root, opener=opener, attempts=attempts)

    def test_both_fixed_models_are_required_and_verified(self):
        specs = {"primary": model("first.bin"), "fallback": model("second.bin")}
        with mock.patch.dict(deps.MODELS, specs, clear=True):
            result = deps.prepare_models(self.root, opener=lambda *a, **k: Response(DATA))
        self.assertTrue(result["verified"])
        for key in specs:
            self.assertEqual(Path(result[key]).read_bytes(), DATA)
            self.assertEqual(Path(result[key]).stat().st_mode & 0o777, 0o644)
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_existing_verified_model_requires_no_network(self):
        (self.root / self.spec.filename).write_bytes(DATA)
        opener = mock.Mock(side_effect=AssertionError("network not allowed"))
        self.assertTrue(self.prepare(opener)["verified"])
        opener.assert_not_called()

    def test_existing_unknown_model_is_preserved_and_rejected(self):
        target = self.root / self.spec.filename
        target.write_bytes(b"unknown")
        opener = mock.Mock()
        with self.assertRaisesRegex(deps.DependencyError, "Existing model differs"):
            self.prepare(opener)
        self.assertEqual(target.read_bytes(), b"unknown")
        opener.assert_not_called()

    def test_interruption_retains_private_part_and_next_run_resumes(self):
        with self.assertRaisesRegex(deps.DependencyError, "rerun to resume"):
            self.prepare(lambda *a, **k: Interrupted())
        part = self.root / (self.spec.filename + ".part")
        self.assertEqual(part.read_bytes(), DATA[:11])
        self.assertEqual(part.stat().st_mode & 0o777, 0o600)
        requests = []

        def resume(request, **kwargs):
            requests.append(request)
            return Response(DATA[11:], 206, {"Content-Range": f"bytes 11-{len(DATA)-1}/{len(DATA)}"})

        self.prepare(resume)
        self.assertEqual(requests[0].get_header("Range"), "bytes=11-")
        self.assertEqual((self.root / self.spec.filename).read_bytes(), DATA)

    def test_range_ignored_by_server_restarts_instead_of_appending(self):
        (self.root / (self.spec.filename + ".part")).write_bytes(DATA[:11])
        self.prepare(lambda *a, **k: Response(DATA))
        self.assertEqual((self.root / self.spec.filename).read_bytes(), DATA)

    def test_wrong_content_range_preserves_candidate_without_publishing(self):
        part = self.root / (self.spec.filename + ".part")
        part.write_bytes(DATA[:11])
        with self.assertRaisesRegex(deps.DependencyError, "Invalid model resume"):
            self.prepare(lambda *a, **k: Response(DATA[11:], 206, {"Content-Range": f"bytes 12-{len(DATA)-1}/{len(DATA)}"}))
        self.assertEqual(part.read_bytes(), DATA[:11])
        self.assertFalse((self.root / self.spec.filename).exists())

    def test_416_restarts_partial_download(self):
        (self.root / (self.spec.filename + ".part")).write_bytes(DATA[:11])
        opener = mock.Mock(side_effect=[urllib.error.HTTPError("https://fixture", 416, "range", {}, None), Response(DATA)])
        self.prepare(opener, attempts=2)
        self.assertIsNone(opener.call_args_list[1].args[0].get_header("Range"))

    def test_same_length_wrong_hash_is_rejected_not_published(self):
        with self.assertRaisesRegex(deps.DependencyError, "checksum/size mismatch"):
            self.prepare(lambda *a, **k: Response(b"x" * len(DATA)))
        self.assertFalse((self.root / self.spec.filename).exists())
        self.assertFalse((self.root / (self.spec.filename + ".part")).exists())

    def test_oversized_response_is_rejected(self):
        with self.assertRaisesRegex(deps.DependencyError, "exceeds the pinned size"):
            self.prepare(lambda *a, **k: Response(DATA + b"unexpected"))
        self.assertFalse((self.root / self.spec.filename).exists())

    def test_symlink_candidate_does_not_overwrite_external_file(self):
        external = self.root / "external"
        external.write_bytes(b"keep")
        (self.root / (self.spec.filename + ".part")).symlink_to(external)
        with self.assertRaises(deps.DependencyError):
            self.prepare(mock.Mock())
        self.assertEqual(external.read_bytes(), b"keep")

    def test_public_api_does_not_accept_hash_or_url_overrides(self):
        with self.assertRaises(TypeError):
            deps.prepare_models(self.root, sha1="unknown", url="https://fixture")


class SQPreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "source"
        sq = self.source / "services/sq-music"
        sq.mkdir(parents=True)
        (sq / "Dockerfile").write_text("FROM scratch\n")
        (sq / "prepare_build.py").write_text("# synthetic fixture only\n")
        self.cache = self.root / "cache"
        self.calls = []
        self.origin = deps.SQ_UPSTREAM_URL
        self.revision = ""
        self.dirty = ""
        self.fail_fetch_once = False
        self.hash_patch = mock.patch.dict(deps.SQ_SOURCE_HASHES, {"Fixture.java": hashlib.sha256(b"audited").hexdigest()}, clear=True)
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)

    def command_runner(self, command, **kwargs):
        self.calls.append(command)
        self.assertEqual(kwargs["cwd"], self.source)
        self.assertTrue(kwargs["check"])
        output = ""
        if command[:2] == ["git", "init"]:
            (Path(command[2]) / ".git").mkdir()
        elif command[0] == "git":
            checkout = Path(command[2])
            action = command[3:]
            if action[:2] == ["remote", "get-url"]:
                output = self.origin
            elif action == ["rev-parse", "HEAD"]:
                if not self.revision:
                    raise subprocess.CalledProcessError(128, command)
                output = self.revision
            elif action[:1] == ["fetch"]:
                if self.fail_fetch_once:
                    self.fail_fetch_once = False
                    raise subprocess.CalledProcessError(1, command)
            elif action[:1] == ["checkout"]:
                self.revision = deps.SQ_UPSTREAM_COMMIT
                source = checkout / "src/main/java/Fixture.java"
                source.parent.mkdir(parents=True)
                source.write_bytes(b"audited")
            elif action == ["status", "--porcelain"]:
                output = self.dirty
        elif "prepare_build.py" in command[1]:
            target = Path(command[-1])
            target.mkdir()
            (target / "Fixture.java").write_bytes(b"patched fixture")
        elif command[:2] == ["docker", "build"]:
            context = Path(command[-1])
            self.assertTrue((context / "patched/Fixture.java").is_file())
            self.assertTrue((context / "Dockerfile").is_file())
            self.assertFalse((context / ".git").exists())
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    def prepare(self):
        return deps.prepare_sq(self.source, self.cache, "fixture/sq:reviewed", runner=self.command_runner)

    def test_fetch_fixed_commit_verify_sources_and_build_external_context(self):
        result = self.prepare()
        self.assertEqual(result["image"], "fixture/sq:reviewed")
        self.assertEqual(result["source_commit"], deps.SQ_UPSTREAM_COMMIT)
        fetch = next(c for c in self.calls if "fetch" in c)
        self.assertEqual(fetch[-1], deps.SQ_UPSTREAM_COMMIT)
        self.assertFalse((self.source / "services/sq-music/patched").exists())
        self.assertEqual(list(self.cache.glob("sq-build-*")), [])

    def test_second_run_reuses_exact_clean_source_without_fetch(self):
        self.prepare()
        self.calls.clear()
        self.prepare()
        self.assertFalse(any("fetch" in c for c in self.calls))

    def test_network_failure_can_resume_without_resetting_checkout(self):
        self.fail_fetch_once = True
        with self.assertRaises(deps.DependencyError):
            self.prepare()
        self.prepare()
        self.assertEqual(sum(c[:2] == ["git", "init"] for c in self.calls), 1)

    def test_other_origin_is_rejected_without_fetch_or_build(self):
        self.origin = "https://fixture.invalid/untrusted.git"
        with self.assertRaisesRegex(deps.DependencyError, "origin differs"):
            self.prepare()
        self.assertFalse(any(c[0] == "docker" or "fetch" in c for c in self.calls))

    def test_dirty_or_changed_source_is_not_overwritten_or_built(self):
        result = self.prepare()
        self.calls.clear()
        (Path(result["source_dir"]) / "src/main/java/Fixture.java").write_bytes(b"modified")
        with self.assertRaisesRegex(deps.DependencyError, "source differs"):
            self.prepare()
        self.assertFalse(any(c[0] == "docker" for c in self.calls))

    def test_wrong_commit_is_preserved(self):
        self.revision = "f" * 40
        with self.assertRaisesRegex(deps.DependencyError, "another revision"):
            self.prepare()
        self.assertFalse(any("checkout" in c for c in self.calls))

    def test_generated_sources_never_go_inside_checkout(self):
        with self.assertRaisesRegex(deps.DependencyError, "outside the source"):
            deps.prepare_sq(self.source, self.source / "cache", "fixture/sq:reviewed", runner=self.command_runner)


if __name__ == "__main__":
    unittest.main()
