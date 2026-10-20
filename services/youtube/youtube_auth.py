#!/usr/bin/env python3
"""Operate the existing YouTube login browser using the stack's active images."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ops.stackctl import Stack  # noqa: E402

BROWSER = "youtube-auth-browser"


def public_probe_url(value: str) -> str:
    url = urlsplit(value)
    host = (url.hostname or "").casefold()
    if (url.scheme != "https" or url.username or url.password or url.port not in (None, 443)
            or host not in {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"}):
        raise argparse.ArgumentTypeError("Use a public HTTPS YouTube link without login credentials.")
    return value


def stop_browser(stack: Stack) -> None:
    rows = stack.inventory()
    if BROWSER not in rows:
        raise RuntimeError("Login browser is not created; run start once before exporting.")
    if rows[BROWSER]["running"]:
        stack.compose("stop", "--timeout", "30", BROWSER)
    stopped = stack.inventory().get(BROWSER)
    if not stopped or stopped["running"]:
        raise RuntimeError("Cannot confirm browser stopped; existing cookies were left unchanged.")


def operate(stack: Stack, command: str, url: str | None) -> None:
    if command == "start":
        stack.compose("up", "-d", "--no-deps", "--no-build", BROWSER)
        print("Complete login and 2FA yourself in the browser; then run export URL.")
        return
    if command == "stop":
        stop_browser(stack)
        return
    exporter = ["exec", "-T", "bot", "python", "/app/nas_youtube_auth.py"]
    if command == "status":
        stack.compose(*exporter, "--status")
        return
    if command not in {"export", "rollback"} or not url:
        raise ValueError("export/rollback requires a public YouTube probe link")
    stop_browser(stack)
    arguments = ["--browser-stopped", "--probe-url", url]
    if command == "rollback":
        arguments.append("--rollback")
    stack.compose(*exporter, *arguments)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-heal", action="store_true", help="preserve the deployed self-heal overlay")
    parser.add_argument("--auth-refresh", action="store_true", help="preserve the explicit auth-refresh overlay")
    parser.add_argument("--profile", action="append", choices=["lx", "sq"], default=[])
    parser.add_argument("command", choices=["start", "stop", "status", "export", "rollback"])
    parser.add_argument("url", nargs="?", type=public_probe_url)
    args = parser.parse_args()
    if (args.command in {"export", "rollback"}) != bool(args.url):
        parser.error("only export/rollback require a public HTTPS YouTube link")
    args.profile.append("auth")
    os.umask(0o077)
    stack = Stack(args)
    operate(stack, args.command, args.url)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError):
        # Expanded Compose configuration can contain secrets; never echo it.
        print("YouTube authentication operation failed; inspect private local service logs.", file=sys.stderr)
        raise SystemExit(1)
