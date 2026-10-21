#!/usr/bin/env python3
"""Download a checked release and run its NAS installer using Python's stdlib."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

REPOSITORY = "zskruc/apple-music-downloader-bot"
RELEASE = "v0.2.0"
PREFIX = "apple-music-downloader-bot"
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 96 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024


class BootstrapError(RuntimeError):
    pass


def checked_root(raw: Path) -> Path:
    path = raw.expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise BootstrapError("Use an absolute dedicated installation directory.")
    if len(path.parts) < 3 or path.name in {'docker', 'LocalWork', 'Documents', 'Desktop'}:
        raise BootstrapError("Use a dedicated directory below the shared storage root.")
    if any(part in {'Documents', 'Desktop', 'iCloud Drive', 'CloudStorage'} for part in path.parts):
        raise BootstrapError("Use a local deployment disk outside cloud-synced folders.")
    if (path / '.git').exists():
        raise BootstrapError("Use a data directory outside the development checkout.")
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise BootstrapError("Installation paths must not traverse symbolic links.")
    return path


def download(url: str, destination: Path, limit: int) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "amdl-release-bootstrap"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("xb") as output:
        if urllib.parse.urlsplit(response.geturl()).scheme != "https":
            raise BootstrapError("Release download must stay on HTTPS.")
        count = 0
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            count += len(chunk)
            if count > limit:
                raise BootstrapError("Release download exceeds the expected size limit.")
            output.write(chunk)


def unpack(archive: Path, destination: Path, digest: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise BootstrapError("A valid release SHA-256 is required.")
    if archive.stat().st_size > MAX_ARCHIVE or hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise BootstrapError("Release archive verification failed.")
    if destination.exists() or destination.is_symlink():
        raise BootstrapError("An existing source directory will not be replaced.")
    with tempfile.TemporaryDirectory(prefix=".source-stage-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        with tarfile.open(archive, "r:gz") as bundle:
            entries = bundle.getmembers()
            if len(entries) > 10000:
                raise BootstrapError("Release archive has too many entries.")
            paths = set()
            total = 0
            # Validate the complete archive before writing any of its members.
            for item in entries:
                path = PurePosixPath(item.name)
                if (path.is_absolute() or ".." in path.parts or not path.parts
                        or path.parts[0] != PREFIX or "\\" in item.name
                        or not (item.isdir() or item.isfile()) or path in paths):
                    raise BootstrapError("Unsafe or duplicate release archive entry.")
                paths.add(path)
                if any(part in {".git", ".venv", "runtime", "secrets"} for part in path.parts):
                    raise BootstrapError("Private runtime data is not a release source file.")
                if any(part.startswith('.env') and part != '.env.example' for part in path.parts):
                    raise BootstrapError("Private environment files are forbidden in releases.")
                total += item.size
                if item.size < 0 or item.size > MAX_FILE or total > MAX_EXPANDED:
                    raise BootstrapError("Expanded release exceeds its size limits.")
            for item in entries:
                target = stage.joinpath(*PurePosixPath(item.name).parts)
                if item.isdir():
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    with contextlib.closing(bundle.extractfile(item)) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(0o755 if item.mode & 0o111 else 0o644)
        package = stage / PREFIX
        if not (package / "deploy/installer.py").is_file():
            raise BootstrapError("Release does not contain the NAS installer.")
        (package / ".release-source.json").write_text(json.dumps({"release": RELEASE, "sha256": digest}) + "\n")
        package.rename(destination)


def dispatch(source: Path, action: str, root: Path, arguments: list[str]) -> int:
    module = "deploy.manage" if action == "backup" else "deploy.installer"
    if action == "restore":
        action = "install"
        arguments = ["--restore" if value == "--bundle" else value for value in arguments]
    environment = dict(os.environ, PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1")
    environment["PATH"] = "/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")
    return subprocess.call([sys.executable, "-m", module, action, "--root", str(root), *arguments],
                           cwd=source, env=environment)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    actions = {"install", "resume", "status", "doctor", "backup", "restore"}
    action = arguments.pop(0) if arguments and arguments[0] in actions else "install"
    parser = argparse.ArgumentParser(description=__doc__, usage="%(prog)s [install|resume|status|doctor|backup|restore] --root DIRECTORY [options]", epilog=(
        "Installer options include --config FILE, --with-media-servers and --dry-run. "
        "Backup uses --destination DIRECTORY; restore uses --bundle DIRECTORY."))
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--archive", type=Path, help="Use an offline release archive with --sha256")
    parser.add_argument("--sha256", help="Expected SHA-256 of --archive")
    args, remaining = parser.parse_known_args(arguments)
    if bool(args.archive) != bool(args.sha256):
        parser.error("--archive and --sha256 must be supplied together")
    os.umask(0o077)
    root = checked_root(args.root)
    installed = root / "source"
    if installed.is_symlink():
        raise BootstrapError("Installed source must not be a symbolic link.")
    filename = globals().get('__file__', '')
    local = Path(filename).resolve().parent if filename and not filename.startswith('<') else None
    if (installed / "deploy/installer.py").is_file():
        return dispatch(installed, action, root, remaining)
    if local is not None and (local / "deploy/installer.py").is_file() and not args.archive:
        return dispatch(local, action, root, remaining)
    if action in {"status", "doctor", "backup", "resume"}:
        raise BootstrapError("No installed release found at this root.")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".release-download-", dir=root) as temporary:
        if args.archive:
            archive, digest = args.archive.expanduser(), args.sha256
        else:
            archive = Path(temporary) / "source.tar.gz"
            checksum = Path(temporary) / "source.tar.gz.sha256"
            base = "https://github.com/" + REPOSITORY + "/releases/download/" + RELEASE
            download(base + "/source.tar.gz.sha256", checksum, 512)
            line = checksum.read_text().strip()
            match = re.fullmatch(r"([a-f0-9]{64})\s+\*?source\.tar\.gz", line)
            if not match:
                raise BootstrapError("Release checksum metadata is invalid.")
            digest = match.group(1)
            download(base + "/source.tar.gz", archive, MAX_ARCHIVE)
        unpack(archive, installed, digest)
    return dispatch(installed, action, root, remaining)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BootstrapError, OSError, tarfile.TarError, ValueError):
        print("Installation bootstrap failed; existing services and data were not replaced.", file=sys.stderr)
        raise SystemExit(1)
