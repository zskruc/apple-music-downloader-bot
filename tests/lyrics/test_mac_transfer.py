from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from word_lyrics_automation.mac_worker import (
    MacWorker,
    RemoteQueue,
    verify_downloaded_audio,
)


class MacTransferTests(unittest.TestCase):
    def test_synthetic_dns_is_replaced_only_by_a_fresh_public_aaaa(self) -> None:
        remote = RemoteQueue({"host": "nas.example.com", "identity_file": "/key"})
        response = subprocess.CompletedProcess([], 0, stdout="2001:4860:4860::8888\n")
        with patch("word_lyrics_automation.mac_worker.socket.getaddrinfo", return_value=[(None, None, None, None, ("fd00::37", 22, 0, 0))]), patch("word_lyrics_automation.mac_worker.subprocess.run", return_value=response) as lookup:
            prefix = remote._ssh_prefix()
        self.assertIn("Hostname=2001:4860:4860::8888", prefix)
        self.assertIn("HostKeyAlias=nas.example.com", prefix)
        self.assertIn("-6", prefix)
        self.assertEqual(lookup.call_args.args[0][4:6], ["AAAA", "nas.example.com"])

    def test_failed_public_dns_does_not_reuse_a_previous_nas_address(self) -> None:
        remote = RemoteQueue({"identity_file": "/key"})
        with patch("word_lyrics_automation.mac_worker.socket.getaddrinfo", side_effect=OSError("offline")), patch("word_lyrics_automation.mac_worker.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="")):
            with self.assertRaisesRegex(RuntimeError, "no public AAAA"):
                remote._ssh_prefix()

    def test_downloaded_audio_requires_exact_size_and_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audio = Path(temporary) / "audio.m4a"
            audio.write_bytes(b"verified-audio" * 4096)
            expected = {
                "size": audio.stat().st_size,
                "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "mtime_ns": 123,
            }
            self.assertEqual(verify_downloaded_audio(audio, expected), {
                "size": expected["size"],
                "sha256": expected["sha256"],
            })
            audio.write_bytes(audio.read_bytes() + b"corrupt")
            with self.assertRaises(RuntimeError):
                verify_downloaded_audio(audio, expected)

    def test_runtime_uses_current_python_without_managed_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configured_python = root / "homebrew-python3.12"
            configured_python.write_bytes(b"")
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "identity_file": str(root / "identity"),
                        "uv_path": "/opt/homebrew/bin/uv",
                        "python_path": str(configured_python),
                    }
                ),
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_run(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                calls.append(arguments)
                if arguments[1] == "venv":
                    runtime_python = root / "runtime/bin/python"
                    runtime_python.parent.mkdir(parents=True)
                    runtime_python.write_bytes(b"")
                return subprocess.CompletedProcess(arguments, 0)

            with patch("word_lyrics_automation.mac_worker.subprocess.run", side_effect=fake_run):
                runtime = MacWorker(config).ensure_runtime()

            self.assertEqual(runtime, root / "runtime/bin/python")
            self.assertEqual(
                calls[0],
                [
                    "/opt/homebrew/bin/uv",
                    "venv",
                    "--no-managed-python",
                    "--python",
                    str(configured_python),
                    str(root / "runtime"),
                ],
            )
            self.assertNotIn("3.12", calls[0])
            self.assertTrue((root / "runtime/.mlx-whisper-0.4.3").is_file())

    def test_job_cannot_install_a_missing_runtime_over_the_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(
                json.dumps({"identity_file": str(root / "identity")}),
                encoding="utf-8",
            )
            worker = MacWorker(config)
            with self.assertRaisesRegex(RuntimeError, "network installation is disabled"):
                worker.ensure_runtime(allow_install=False)

    def test_modelscope_mirror_downloads_only_verified_model_data(self) -> None:
        worker = object.__new__(MacWorker)
        worker.config = {
            "modelscope_mirror_enabled": True,
            "modelscope_mirror_base": "https://modelscope.invalid/models",
            "modelscope_revision": "master",
            "modelscope_fallback_to_huggingface": False,
        }
        requested: list[str] = []

        def fake_download(url: str, destination: Path) -> dict[str, object]:
            requested.append(url)
            if destination.name == "config.json":
                destination.write_text('{"model_type":"whisper"}', encoding="utf-8")
            else:
                with zipfile.ZipFile(destination, "w") as archive:
                    archive.writestr("weight.npy", b"weight-data")
            return {"size": destination.stat().st_size, "sha256": "verified"}

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(MacWorker, "_download_model_file", side_effect=fake_download):
                model_path, manifest = worker.prepare_model(
                    "mlx-community/whisper-small-mlx-q4",
                    Path(temporary),
                )

            self.assertEqual(Path(model_path), Path(temporary) / "model")
            self.assertEqual(manifest["source"], "modelscope")
            self.assertEqual(len(requested), 2)
            self.assertTrue(requested[0].endswith("/resolve/master/config.json"))
            self.assertTrue(requested[1].endswith("/resolve/master/weights.npz"))

    def test_retained_model_is_verified_and_reused_without_download(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "identity_file": str(root / "identity"),
                        "model_cache_policy": "retain",
                        "modelscope_mirror_enabled": True,
                        "modelscope_mirror_base": "https://modelscope.invalid/models",
                        "modelscope_revision": "master",
                        "modelscope_fallback_to_huggingface": False,
                    }
                ),
                encoding="utf-8",
            )
            worker = MacWorker(config)
            requested: list[str] = []

            def fake_download(url: str, destination: Path) -> dict[str, object]:
                requested.append(url)
                if destination.name == "config.json":
                    destination.write_text('{"model_type":"whisper"}', encoding="utf-8")
                else:
                    with zipfile.ZipFile(destination, "w") as archive:
                        archive.writestr("weight.npy", b"weight-data")
                data = destination.read_bytes()
                return {
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }

            with patch.object(MacWorker, "_download_model_file", side_effect=fake_download):
                first_path, first = worker.prepare_model(
                    "mlx-community/whisper-large-v3-turbo-q4",
                    root / "work-one",
                )
                second_path, second = worker.prepare_model(
                    "mlx-community/whisper-large-v3-turbo-q4",
                    root / "work-two",
                )

            expected = (
                root / "model-cache/mlx-community--whisper-large-v3-turbo-q4"
            ).resolve()
            self.assertEqual(Path(first_path), expected)
            self.assertEqual(Path(second_path), expected)
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(second["source"], "modelscope_cache")
            self.assertEqual(len(requested), 2)
            self.assertTrue((expected / "model-manifest.json").is_file())

    def test_job_cannot_redownload_a_missing_retained_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "identity_file": str(root / "identity"),
                        "model_cache_policy": "retain",
                        "modelscope_mirror_enabled": True,
                    }
                ),
                encoding="utf-8",
            )
            worker = MacWorker(config)
            with self.assertRaisesRegex(RuntimeError, "model download is disabled"):
                worker.prepare_model(
                    "mlx-community/whisper-large-v3-turbo-q4",
                    root / "work",
                    allow_download=False,
                )

    def test_candidate_upload_enables_container_standard_input(self) -> None:
        remote = object.__new__(RemoteQueue)
        remote.container = "resolver"
        remote.test_state_dir = ""
        ordinary = remote._remote_command(["job", "--job-id", "2"])
        upload = remote._remote_command(
            ["submit-candidate", "--job-id", "2"],
            interactive=True,
        )
        self.assertIn("docker exec resolver", ordinary)
        self.assertNotIn("docker exec -i resolver", ordinary)
        self.assertIn("docker exec -i resolver", upload)

    def test_isolated_state_override_is_passed_only_to_test_queue(self) -> None:
        remote = object.__new__(RemoteQueue)
        remote.container = "resolver"
        remote.test_state_dir = "/state/word-lyrics/automation-tests/e2e"
        command = remote._remote_command(["claim", "--stage", "mac_alignment"])
        self.assertIn(
            "-e AMDL_WORD_LYRICS_STATE_DIR=/state/word-lyrics/automation-tests/e2e",
            command,
        )


if __name__ == "__main__":
    unittest.main()
