"""Pinned, resumable deployment dependencies; no network or Docker work on import.

SQ sources stay in the operator's cache and are never bundled for redistribution.
Model checksums are code constants, not caller-supplied assertions. See SOURCES.md.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import http.client
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Callable
import urllib.error
import urllib.request


DEFAULT_IMAGES = {
    "SQ_MYSQL_IMAGE": "mysql@sha256:4bc6bc963e6d8443453676cae56536f4b8156d78bae03c0145cbe47c2aad73bb",
    "SQ_NETEASE_IMAGE": "registry.cn-hangzhou.aliyuncs.com/sqdockler/netease_cloud_music_api@sha256:87e4c7bf459da928309fd3c8840142f0f005b6a0f8812f5360acb3eac1e8553c",
    "SQ_BASE_IMAGE": "ghcr.io/59799517/simple_sq_music_plus@sha256:4f3e499c07da526bea5648fc4e1b9c449e363356c5e8c1ef4d96c2d67b1e20f2",
    "LX_IMAGE": "ghcr.io/xcq0607/lxserver@sha256:aa3db966049584585639ca6563e85ce5b7ebf33569dd1f5064c21bd7330382af",
    "AUTH_BROWSER_IMAGE": "linuxserver/chromium@sha256:9761e4dce2375165ab85566d5a58920525d49c49469ed7a08a6d874dcdb1cedc",
    "PO_PROVIDER_IMAGE": "brainicism/bgutil-ytdlp-pot-provider@sha256:d7d34108b76e52c7d3b575fe9e797e4bf41c1b2a08f80c5292eff6f851ce8472",
    "NAVIDROME_IMAGE": "navidrome/navidrome@sha256:38dc2727bfcfd5ede290f8ada114fc90368146f265ae4701ddddbcbe2a44ee52",
    "EMBY_IMAGE": "emby/embyserver@sha256:7082c63b10a2fcb3805a7eaebba407d5723bc57c085c9f0b29b7d0a702765cc1",
    "LRCAPI_IMAGE": "hisatri/lrcapi@sha256:9efbf412d3c0f87e2f090954865666df2268ae2f14f104f9f430868e6abef2de",
}
SQ_UPSTREAM_URL = "https://github.com/59799517/simple_sq_music_plus.git"
SQ_UPSTREAM_COMMIT = "cf0f6ed136be0aa13845bbab028c27e404b89d5d"
SQ_SOURCE_HASHES = {
    "com/sqmusicplus/v3/plug/base/hander/SearchHanderAbstract.java": "4171aab5c07b0db4b22d8bebd1bb79425d2019c0f55b4dbeaf8a45cdf9566538",
    "com/sqmusicplus/v3/controller/TaskController.java": "96983130c4c80aefce618c2919efb367b28c71c816e9acc50cc29f129ae08b21",
}
MODEL_REVISION = "5359861c739e955e79d9a303bcbc70fb988958b1"


@dataclass(frozen=True)
class Model:
    filename: str
    sha1: str
    sha256: str
    size: int

    @property
    def url(self) -> str:
        return f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{MODEL_REVISION}/{self.filename}"


MODELS = {
    "primary": Model("ggml-large-v3-turbo-q5_0.bin", "e050f7970618a659205450ad97eb95a18d69c9ee",
                     "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2", 574041195),
    "fallback": Model("ggml-large-v3-q5_0.bin", "e6e2ed78495d403bef4b7cff42ef4aaadcfea8de",
                      "d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1", 1081140203),
}


class DependencyError(RuntimeError):
    """A safe summary, without potentially sensitive transport diagnostics."""


def _regular_or_absent(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise DependencyError(f"Refusing non-regular dependency file: {path.name}")


@contextmanager
def _lock(path: Path):
    _regular_or_absent(path)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _valid_model(path: Path, spec: Model) -> bool:
    _regular_or_absent(path)
    if not path.is_file() or path.stat().st_size != spec.size:
        return False
    sha1, sha256 = hashlib.sha1(), hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            sha1.update(chunk)
            sha256.update(chunk)
    return sha1.hexdigest() == spec.sha1 and sha256.hexdigest() == spec.sha256


def _publish_model(part: Path, destination: Path, spec: Model) -> None:
    if not _valid_model(part, spec):
        part.unlink(missing_ok=True)
        raise DependencyError(f"Model checksum/size mismatch: {spec.filename}")
    part.chmod(0o644)
    os.replace(part, destination)
    directory = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _prepare_model(directory: Path, spec: Model, opener: Callable, attempts: int) -> Path:
    destination = directory / spec.filename
    part = directory / (spec.filename + ".part")
    with _lock(directory / (spec.filename + ".lock")):
        _regular_or_absent(destination)
        _regular_or_absent(part)
        if destination.exists():
            if not _valid_model(destination, spec):
                raise DependencyError(f"Existing model differs from the pinned file: {spec.filename}")
            return destination
        if part.exists() and _valid_model(part, spec):
            _publish_model(part, destination, spec)
            return destination
        for _ in range(attempts):
            offset = part.stat().st_size if part.exists() else 0
            if offset >= spec.size:
                part.unlink()
                offset = 0
            request = urllib.request.Request(spec.url, headers={"User-Agent": "AppleMusicDownloader-Setup/1"})
            if offset:
                request.add_header("Range", f"bytes={offset}-")
            try:
                with opener(request, timeout=60) as response:
                    status = response.status
                    if status == 206:
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                        if (not match or int(match[1]) != offset or int(match[2]) < offset
                                or int(match[3]) != spec.size or int(match[2]) >= spec.size):
                            raise DependencyError("Invalid model resume response; partial file preserved")
                        mode = "ab" if offset else "wb"
                    elif status == 200:
                        # Servers may ignore Range; restart rather than append a full file.
                        offset, mode = 0, "wb"
                    else:
                        raise DependencyError("Unexpected model download response")
                    fd = os.open(part, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                                 | (os.O_APPEND if mode == "ab" else os.O_TRUNC), 0o600)
                    with os.fdopen(fd, mode) as output:
                        os.fchmod(output.fileno(), 0o600)
                        total = offset
                        while True:
                            try:
                                chunk = response.read(1024 * 1024)
                            except http.client.IncompleteRead as exc:
                                if exc.partial:
                                    output.write(exc.partial)
                                raise
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > spec.size:
                                raise DependencyError("Model transfer exceeds the pinned size")
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                if part.stat().st_size < spec.size:
                    continue
                _publish_model(part, destination, spec)
                return destination
            except urllib.error.HTTPError as exc:
                if exc.code == 416:
                    part.unlink(missing_ok=True)
                    continue
                if exc.code not in {408, 429, 500, 502, 503, 504}:
                    raise DependencyError("Model source rejected the request") from None
            except (urllib.error.URLError, OSError, http.client.IncompleteRead):
                # Keep verified-position partial bytes for this invocation or a later resume.
                continue
        raise DependencyError(f"Model transfer interrupted; rerun to resume: {spec.filename}")


def prepare_models(path: Path, *, opener: Callable | None = None, attempts: int = 3) -> dict:
    """Prepare both exact worker models, with resumable transfers and atomic publish.

    Network injection is for tests; production exposes no hash or source override.
    """
    directory = Path(path).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if not 1 <= attempts <= 10:
        raise ValueError("attempts must be between 1 and 10")
    result = {role: str(_prepare_model(directory, spec, opener or urllib.request.urlopen, attempts))
              for role, spec in MODELS.items()}
    return {**result, "verified": True}


def _run(command: list[str], cwd: Path, runner: Callable, timeout: int = 600) -> str:
    try:
        completed = runner(command, cwd=cwd, check=True, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return (completed.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        raise DependencyError(f"Dependency preparation failed at {command[0]}; inspect private local diagnostics") from None


def _verify_sq_sources(checkout: Path) -> None:
    for relative, expected in SQ_SOURCE_HASHES.items():
        source = checkout / "src/main/java" / relative
        _regular_or_absent(source)
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise DependencyError("SQ source differs from the audited commit; refusing patch/build")


def prepare_sq(source_dir: Path, cache_dir: Path, image: str, *, runner: Callable | None = None) -> dict:
    """Fetch the fixed SQ commit and locally build the existing original patch.

    No upstream Java source, generated patch or image is redistributed here.
    A failed fetch is resumable; an existing checkout is never reset or cleaned.
    """
    source_dir = Path(source_dir).expanduser().resolve()
    cache_dir = Path(cache_dir).expanduser().resolve()
    if cache_dir == source_dir or source_dir in cache_dir.parents:
        raise DependencyError("SQ cache must be outside the source checkout")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._/:-]*", image) or "@" in image:
        raise ValueError("Use a valid local image tag for SQ")
    sq = source_dir / "services/sq-music"
    if not (sq / "prepare_build.py").is_file() or not (sq / "Dockerfile").is_file():
        raise DependencyError("SQ build helpers are missing from the source checkout")
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    runner = runner or subprocess.run
    checkout = cache_dir / ("sq-source-" + SQ_UPSTREAM_COMMIT[:12])
    with _lock(cache_dir / ".sq-prepare.lock"):
        if checkout.is_symlink() or (checkout.exists() and not checkout.is_dir()):
            raise DependencyError("Refusing non-directory SQ source cache")
        if not checkout.exists():
            checkout.mkdir(mode=0o700)
        if (checkout / ".git").is_symlink():
            raise DependencyError("Refusing a symlinked SQ Git directory")
        if not (checkout / ".git").exists():
            if any(checkout.iterdir()):
                raise DependencyError("SQ cache contains unrecognized files")
            _run(["git", "init", str(checkout)], source_dir, runner)
            _run(["git", "-C", str(checkout), "remote", "add", "origin", SQ_UPSTREAM_URL], source_dir, runner)
        origin = _run(["git", "-C", str(checkout), "remote", "get-url", "origin"], source_dir, runner)
        if origin != SQ_UPSTREAM_URL:
            raise DependencyError("SQ source-cache origin differs from the pinned upstream")
        try:
            revision = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"], source_dir, runner)
        except DependencyError:
            revision = ""
        if revision != SQ_UPSTREAM_COMMIT:
            if revision:
                raise DependencyError("Existing SQ checkout uses another revision; preserve it separately")
            _run(["git", "-C", str(checkout), "fetch", "--depth", "1", "origin", SQ_UPSTREAM_COMMIT], source_dir, runner)
            _run(["git", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"], source_dir, runner)
        if _run(["git", "-C", str(checkout), "rev-parse", "HEAD"], source_dir, runner) != SQ_UPSTREAM_COMMIT:
            raise DependencyError("Fetched SQ commit differs from the fixed revision")
        if _run(["git", "-C", str(checkout), "status", "--porcelain"], source_dir, runner):
            raise DependencyError("SQ source cache has local changes; refusing to overwrite them")
        _verify_sq_sources(checkout)
        with tempfile.TemporaryDirectory(prefix="sq-build-", dir=cache_dir) as work:
            context = Path(work)
            shutil.copyfile(sq / "Dockerfile", context / "Dockerfile")
            _run([sys.executable, str(sq / "prepare_build.py"), str(checkout),
                  "--output", str(context / "patched")], source_dir, runner)
            _run(["docker", "build", "--tag", image, str(context)], source_dir, runner, timeout=3600)
        return {"image": image, "source_commit": SQ_UPSTREAM_COMMIT, "source_dir": str(checkout)}
