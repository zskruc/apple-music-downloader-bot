from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path.home() / "Library/Application Support/WordLyrics/config.json"


def verify_downloaded_audio(
    path: Path,
    expected: dict[str, Any],
    *,
    chunk_size: int = 1024 * 1024,
) -> dict[str, int | str]:
    """Verify the streamed copy before any model is downloaded or started."""

    expected_size = int(expected.get("size") or -1)
    expected_sha256 = str(expected.get("sha256") or "")
    if expected_size < 0 or len(expected_sha256) != 64:
        raise RuntimeError("queue job has no complete source fingerprint")
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise RuntimeError(
            f"downloaded audio size mismatch: expected={expected_size}, actual={actual_size}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError("downloaded audio SHA-256 mismatch")
    return {"sha256": actual_sha256, "size": actual_size}


class RemoteQueue:
    def __init__(self, config: dict[str, Any]) -> None:
        self.host = str(config.get("host") or os.environ.get("WORD_LYRICS_NAS_HOST", "nas.example.com"))
        self.user = str(config.get("user") or os.environ.get("WORD_LYRICS_NAS_USER", "nas"))
        self.key = str(config["identity_file"])
        self.container = str(
            config.get("resolver_container")
            or "apple-music-downloader-word-lyrics-resolver"
        )
        self.timeout = int(config.get("ssh_connect_timeout_seconds") or 20)
        self.test_state_dir = str(config.get("test_remote_state_dir") or "").strip()
        if self.test_state_dir and not self.test_state_dir.startswith(
            "/state/word-lyrics/automation-tests/"
        ):
            raise RuntimeError(
                "isolated remote state must stay inside /state/word-lyrics/automation-tests"
            )

    def resolve_current_aaaa(self) -> list[str]:
        def public_ipv6(values: list[str]) -> list[str]:
            addresses = set()
            for value in values:
                try:
                    address = ipaddress.ip_address(value.strip())
                except ValueError:
                    continue
                if address.version == 6 and address.is_global:
                    addresses.add(str(address))
            return sorted(addresses)

        try:
            addresses = public_ipv6([
                item[4][0] for item in socket.getaddrinfo(
                    self.host, 22, family=socket.AF_INET6, type=socket.SOCK_STREAM,
                )
            ])
        except OSError:
            addresses = []
        if not addresses:
            # Some local proxy DNS servers synthesize private IPv6 addresses.
            # Resolve the domain afresh through public DNS; never use a saved
            # NAS address or a mesh-network endpoint.
            for resolver in ("223.5.5.5", "1.1.1.1"):
                try:
                    response = subprocess.run(
                        ["dig", "+short", "+time=3", "+tries=1", "AAAA", self.host, f"@{resolver}"],
                        capture_output=True, text=True, timeout=5, check=True,
                    )
                    addresses = public_ipv6(response.stdout.splitlines())
                except (OSError, subprocess.SubprocessError):
                    continue
                if addresses:
                    break
        if not addresses:
            raise RuntimeError(f"{self.host} currently has no public AAAA record for direct IPv6")
        return addresses

    def _ssh_prefix(self) -> list[str]:
        addresses = self.resolve_current_aaaa()
        return [
            "/usr/bin/ssh",
            "-6",
            "-i",
            self.key,
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.timeout}",
            "-o",
            f"Hostname={addresses[0]}",
            "-o",
            f"HostKeyAlias={self.host}",
            f"{self.user}@{self.host}",
        ]

    def _remote_command(
        self,
        arguments: list[str],
        *,
        interactive: bool = False,
    ) -> str:
        docker_arguments = ["exec"]
        if interactive:
            docker_arguments.append("-i")
        if self.test_state_dir:
            docker_arguments.extend(
                ["-e", f"AMDL_WORD_LYRICS_STATE_DIR={self.test_state_dir}"]
            )
        return shlex.join(
            [
                "sudo",
                "-n",
                "/usr/local/bin/docker",
                *docker_arguments,
                self.container,
                "python",
                "-m",
                "word_lyrics_automation.queue_cli",
                *arguments,
            ]
        )

    def json(self, arguments: list[str], *, input_bytes: bytes | None = None) -> Any:
        process = subprocess.run(
            [
                *self._ssh_prefix(),
                self._remote_command(
                    arguments,
                    interactive=input_bytes is not None,
                ),
            ],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if process.returncode:
            raise RuntimeError(
                f"remote queue command failed ({process.returncode}): "
                f"{process.stderr.decode(errors='replace')[-1000:]}"
            )
        return json.loads(process.stdout.decode("utf-8"))

    def export_audio(self, job: dict[str, Any], destination: Path) -> None:
        command = self._remote_command(
            [
                "export-audio",
                "--job-id",
                str(job["id"]),
                "--token",
                str(job["lease_token"]),
                "--stage",
                "mac_alignment",
            ]
        )
        with destination.open("wb") as output:
            process = subprocess.run(
                [*self._ssh_prefix(), command],
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=7200,
                check=False,
            )
        if process.returncode:
            destination.unlink(missing_ok=True)
            raise RuntimeError(
                f"audio export failed ({process.returncode}): "
                f"{process.stderr.decode(errors='replace')[-1000:]}"
            )


class LeaseHeartbeat:
    def __init__(self, remote: RemoteQueue, job: dict[str, Any], interval: float) -> None:
        self.remote = remote
        self.job = job
        self.interval = max(30.0, interval)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stopped.wait(self.interval):
            try:
                self.remote.json(
                    [
                        "heartbeat",
                        "--job-id",
                        str(self.job["id"]),
                        "--token",
                        str(self.job["lease_token"]),
                        "--lease-seconds",
                        "2700",
                    ]
                )
            except Exception:
                # The lease is deliberately long; a temporary network failure is
                # retried by the next heartbeat and expiry recovery is server-side.
                pass

    def __enter__(self) -> "LeaseHeartbeat":
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stopped.set()
        self.thread.join(timeout=5)


class MacWorker:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.remote = RemoteQueue(self.config)
        self.owner = f"mac:{socket.gethostname()}:{os.getpid()}"
        self.app_dir = config_path.parent
        self.runtime = self.app_dir / "runtime"
        self.logs = self.app_dir / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)

    def ensure_runtime(self, *, allow_install: bool = True) -> Path:
        python = self.runtime / "bin/python"
        marker = self.runtime / ".mlx-whisper-0.4.3"
        if python.is_file() and marker.is_file():
            return python
        if not allow_install:
            raise RuntimeError(
                "Mac alignment runtime is missing; automatic network installation is disabled"
            )
        uv = str(self.config.get("uv_path") or "/opt/homebrew/bin/uv")
        # ``uv venv --python 3.12`` is allowed to prefer a uv-managed Python.
        # On a clean Mac that silently turns a small local venv operation into
        # a network download, which can leave a leased lyric job retrying for
        # hours on a slow or filtered connection.  The launch agent is already
        # running under the audited Homebrew Python, so pin the venv to that
        # exact interpreter and explicitly forbid managed-Python downloads.
        runtime_python = str(self.config.get("python_path") or sys.executable)
        if not Path(runtime_python).is_file():
            raise RuntimeError(f"configured runtime Python is missing: {runtime_python}")
        if self.runtime.exists():
            shutil.rmtree(self.runtime)
        subprocess.run(
            [
                uv,
                "venv",
                "--no-managed-python",
                "--python",
                runtime_python,
                str(self.runtime),
            ],
            check=True,
            timeout=120,
        )
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "mlx-whisper==0.4.3",
            ],
            check=True,
            timeout=1800,
        )
        marker.touch()
        return python

    @staticmethod
    def _download_model_file(url: str, destination: Path) -> dict[str, Any]:
        temporary = destination.with_name(destination.name + ".part")
        digest = hashlib.sha256()
        size = 0
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "WordLyrics/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                expected_size = int(response.headers.get("Content-Length") or 0)
                with temporary.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            if expected_size and size != expected_size:
                raise RuntimeError(
                    f"model file size mismatch for {destination.name}: "
                    f"expected={expected_size}, actual={size}"
                )
            if size <= 0:
                raise RuntimeError(f"empty model file: {destination.name}")
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return {"size": size, "sha256": digest.hexdigest()}

    def prepare_model(
        self,
        model: str,
        work: Path,
        *,
        allow_download: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        if not bool(self.config.get("modelscope_mirror_enabled", True)):
            return model, {"source": "huggingface"}
        components = model.split("/", 1)
        if len(components) != 2 or not all(
            component
            and all(character.isalnum() or character in "._-" for character in component)
            for component in components
        ):
            return model, {"source": "configured_path"}
        base = str(
            self.config.get("modelscope_mirror_base")
            or "https://modelscope.cn/models"
        ).rstrip("/")
        revision = str(self.config.get("modelscope_revision") or "master")
        cache_policy = str(self.config.get("model_cache_policy") or "ephemeral")
        if cache_policy == "retain":
            cache_root = (self.app_dir / "model-cache").resolve()
            configured_path = str(self.config.get("model_storage_path") or "").strip()
            model_dir = (
                Path(configured_path).expanduser().resolve()
                if configured_path
                else cache_root / model.replace("/", "--")
            )
            try:
                model_dir.relative_to(cache_root)
            except ValueError as exc:
                raise RuntimeError(
                    "retained model path must stay inside the application model-cache"
                ) from exc
        else:
            model_dir = work / "model"

        cached_manifest = self._validated_model_manifest(
            model_dir,
            repository=model,
            revision=revision,
        )
        if cached_manifest is not None:
            cached_manifest["source"] = "modelscope_cache"
            cached_manifest["cached"] = True
            return str(model_dir), cached_manifest
        if cache_policy == "retain" and not allow_download:
            raise RuntimeError(
                "retained Mac model is missing or corrupt; automatic model download is disabled"
            )

        model_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{model_dir.name}.download-",
                dir=model_dir.parent,
            )
        )
        manifest: dict[str, Any] = {
            "source": "modelscope",
            "repository": model,
            "revision": revision,
            "files": {},
            "cached": False,
        }
        try:
            for filename in ("config.json", "weights.npz"):
                url = (
                    f"{base}/{urllib.parse.quote(components[0], safe='')}"
                    f"/{urllib.parse.quote(components[1], safe='')}"
                    f"/resolve/{urllib.parse.quote(revision, safe='')}"
                    f"/{filename}"
                )
                manifest["files"][filename] = self._download_model_file(
                    url,
                    staging / filename,
                )
            json.loads((staging / "config.json").read_text(encoding="utf-8"))
            with zipfile.ZipFile(staging / "weights.npz") as archive:
                corrupt_member = archive.testzip()
                if corrupt_member is not None:
                    raise RuntimeError(f"corrupt model member: {corrupt_member}")
            manifest_path = staging / "model-manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with manifest_path.open("rb") as handle:
                os.fsync(handle.fileno())
            if model_dir.exists():
                shutil.rmtree(model_dir)
            os.replace(staging, model_dir)
            return str(model_dir), manifest
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if not bool(self.config.get("modelscope_fallback_to_huggingface", True)):
                raise
            print(
                "ModelScope mirror failed; falling back to Hugging Face: "
                f"{type(exc).__name__}:{exc}",
                file=sys.stderr,
                flush=True,
            )
            return model, {
                "source": "huggingface_fallback",
                "mirror_error": f"{type(exc).__name__}:{str(exc)[:300]}",
            }

    @staticmethod
    def _validated_model_manifest(
        model_dir: Path,
        *,
        repository: str,
        revision: str,
    ) -> dict[str, Any] | None:
        manifest_path = model_dir / "model-manifest.json"
        config_path = model_dir / "config.json"
        weights_path = model_dir / "weights.npz"
        if not all(path.is_file() for path in (manifest_path, config_path, weights_path)):
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("repository") != repository:
                return None
            if manifest.get("revision") != revision:
                return None
            json.loads(config_path.read_text(encoding="utf-8"))
            files = manifest.get("files")
            if not isinstance(files, dict):
                return None
            for filename, path in (
                ("config.json", config_path),
                ("weights.npz", weights_path),
            ):
                expected = files.get(filename)
                if not isinstance(expected, dict):
                    return None
                if path.stat().st_size != int(expected.get("size") or -1):
                    return None
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest() != str(expected.get("sha256") or ""):
                    return None
            with zipfile.ZipFile(weights_path) as archive:
                if archive.testzip() is not None:
                    return None
            return manifest
        except (OSError, ValueError, TypeError, json.JSONDecodeError, zipfile.BadZipFile):
            return None

    def prepare_assets(self) -> dict[str, Any]:
        python = self.ensure_runtime(allow_install=True)
        with tempfile.TemporaryDirectory(prefix="word-lyrics-prepare-") as temporary:
            configured_model = str(
                self.config.get("model")
                or "mlx-community/whisper-large-v3-turbo-q4"
            )
            model_path, delivery = self.prepare_model(
                configured_model,
                Path(temporary),
                allow_download=True,
            )
        subprocess.run(
            [str(python), "-c", "import mlx_whisper"],
            check=True,
            timeout=120,
        )
        return {
            "runtime_python": str(python),
            "model": configured_model,
            "model_path": model_path,
            "model_delivery": delivery,
        }

    def run_once(self) -> bool:
        job = self.remote.json(
            [
                "claim",
                "--stage",
                "mac_alignment",
                "--owner",
                self.owner,
                "--lease-seconds",
                "2700",
            ]
        )
        if job is None:
            return False
        try:
            with tempfile.TemporaryDirectory(prefix="word-lyrics-") as temporary:
                work = Path(temporary)
                suffix = Path(str(job["media_path"])).suffix or ".audio"
                audio = work / f"source{suffix}"
                payload_path = work / "payload.json"
                candidate = work / "candidate.ttml"
                report_path = work / "report.json"
                payload_path.write_text(
                    json.dumps(job.get("payload") or {}, ensure_ascii=False),
                    encoding="utf-8",
                )
                with LeaseHeartbeat(self.remote, job, interval=60):
                    python = self.ensure_runtime(
                        allow_install=bool(
                            self.config.get("allow_runtime_install_during_job", False)
                        )
                    )
                    model_cache_policy = str(
                        self.config.get("model_cache_policy") or "ephemeral"
                    )
                    if model_cache_policy == "retain":
                        model_cache = self.app_dir / "model-cache"
                        model_cache.mkdir(parents=True, exist_ok=True)
                    else:
                        model_cache = work / "model-cache"
                    environment = dict(os.environ)
                    ffmpeg_path = Path(
                        str(
                            self.config.get("ffmpeg_path")
                            or "/opt/homebrew/bin/ffmpeg"
                        )
                    )
                    if not ffmpeg_path.is_file():
                        raise RuntimeError(
                            f"configured ffmpeg is missing: {ffmpeg_path}"
                        )
                    executable_path = environment.get(
                        "PATH",
                        "/usr/bin:/bin:/usr/sbin:/sbin",
                    )
                    environment.update(
                        {
                            "PATH": f"{ffmpeg_path.parent}:{executable_path}",
                            "PYTHONPATH": str(self.app_dir / "src"),
                            "HF_HOME": str(model_cache),
                            "HUGGINGFACE_HUB_CACHE": str(model_cache / "hub"),
                            # The Xet transport can collapse to a single very
                            # slow range request on filtered home networks.
                            # Plain HTTPS is slower than Xet on ideal links but
                            # far more predictable for this bounded worker.
                            "HF_HUB_DISABLE_XET": str(
                                self.config.get("disable_hf_xet", True)
                            ).lower(),
                        }
                    )
                    duration = float((job.get("payload") or {}).get("track", {}).get("duration") or 0)
                    timeout = max(
                        int(self.config.get("alignment_timeout_seconds") or 7200),
                        int(duration * 8 + 900),
                    )
                    configured_model = str(
                        self.config.get("model")
                        or "mlx-community/whisper-small-mlx-q4"
                    )
                    model_path, model_delivery = self.prepare_model(
                        configured_model,
                        work,
                        allow_download=bool(
                            self.config.get("allow_model_download_during_job", False)
                        ),
                    )
                    self.remote.export_audio(job, audio)
                    downloaded_fingerprint = verify_downloaded_audio(
                        audio,
                        job.get("fingerprint_data") or {},
                    )
                    subprocess.run(
                        [
                            str(python),
                            "-m",
                            "word_lyrics_automation.alignment_backend_mlx",
                            "--audio",
                            str(audio),
                            "--payload",
                            str(payload_path),
                            "--output",
                            str(candidate),
                            "--report",
                            str(report_path),
                            "--model",
                            model_path,
                        ],
                        env=environment,
                        check=True,
                        timeout=timeout,
                    )
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    report["model"] = configured_model
                    report["model_delivery"] = model_delivery
                    report["downloaded_fingerprint"] = downloaded_fingerprint
                    report_b64 = base64.urlsafe_b64encode(
                        json.dumps(report, ensure_ascii=False).encode("utf-8")
                    ).decode("ascii").rstrip("=")
                    self.remote.json(
                        [
                            "submit-candidate",
                            "--job-id",
                            str(job["id"]),
                            "--token",
                            str(job["lease_token"]),
                            "--stage",
                            "mac_alignment",
                            "--report-b64",
                            report_b64,
                        ],
                        input_bytes=candidate.read_bytes(),
                    )
            return True
        except Exception as exc:
            try:
                self.remote.json(
                    [
                        "fail",
                        "--job-id",
                        str(job["id"]),
                        "--token",
                        str(job["lease_token"]),
                        "--stage",
                        "mac_alignment",
                        "--error",
                        f"{type(exc).__name__}:{str(exc)[:800]}",
                    ]
                )
            except Exception:
                pass
            raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    worker = MacWorker(args.config)
    if args.prepare_only:
        print(json.dumps(worker.prepare_assets(), ensure_ascii=False, indent=2))
    else:
        worker.run_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
