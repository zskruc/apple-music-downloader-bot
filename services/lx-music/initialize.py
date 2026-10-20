#!/usr/bin/env python3
"""Initialize a new private LXServer state directory without printing secrets.

For existing installations, keep the existing config, users, playlists, and
frontend password. This command deliberately refuses to overwrite any of them.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets


def initialize(data_path: Path, auth_file: Path) -> None:
    config = data_path / "data" / "config.js"
    if config.exists() or auth_file.exists():
        raise ValueError("config or authentication file already exists; preserve the existing installation")
    if (data_path / "data").exists() and any((data_path / "data").iterdir()):
        raise ValueError("data directory is not empty; use the existing-installation instructions")
    if config.absolute() == auth_file.absolute():
        raise ValueError("configuration and authentication paths must differ")

    password = secrets.token_urlsafe(48)
    settings = {
        "serverName": "apple-music-lx",
        "debug.enabled": False,
        "frontend.password": password,
        "player.enableAuth": True,
        "player.password": password,
        "users": [],
        "disableTelemetry": True,
        "user.enablePublicRestriction": True,
        "user.enablePublicFavorites": False,
        "user.enablePublicNonAdminAccess": False,
        "user.enablePublicNonAdminLocalMusic": False,
        "user.enablePublicNonAdminServerCache": False,
        "user.enablePublicNonAdminBrowserDownload": False,
        "webdav.enable": False,
        "subsonic.enable": False,
    }
    created: list[Path] = []
    old_umask = os.umask(0o077)
    try:
        for directory in ("data", "logs", "cache", "cover_cache"):
            (data_path / directory).mkdir(parents=True, exist_ok=True, mode=0o700)
        auth_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path, content in (
            (auth_file, password + "\n"),
            (config, "module.exports = " + json.dumps(settings, ensure_ascii=False, indent=2) + ";\n"),
        ):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(path)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    finally:
        os.umask(old_umask)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-path", type=Path, default=Path("runtime/lx-music"))
    parser.add_argument("--auth-file", type=Path, default=Path("secrets/lx-frontend-auth"))
    args = parser.parse_args()
    try:
        initialize(args.data_path, args.auth_file)
    except (OSError, ValueError) as exc:
        print(f"LXServer initialization refused: {exc}")
        return 1
    print("LXServer private configuration and authentication file created; secret values omitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
