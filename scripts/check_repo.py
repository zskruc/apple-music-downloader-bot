#!/usr/bin/env python3
"""Check the distributable source tree without printing sensitive matches."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {".git", ".venv", ".venv312", "__pycache__", ".pytest_cache"}
FORBIDDEN_PARTS = {"state", "private", "legacy-vm-data", "wrapper-data", "backups", "logs", "node_modules"}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".wvd", ".qcow2", ".img", ".iso", ".sqlite3", ".db", ".log"}
# This source module handles private Cookie copies; it contains no session data.
COOKIE_SOURCE_FILES = {Path("bot/yt_dlp_readonly_cookies.py")}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"),
    "Telegram token": re.compile(r"(?<![A-Za-z0-9])\d{7,13}:[A-Za-z0-9_-]{30,50}(?![A-Za-z0-9_-])"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b"),
}


def main() -> int:
    failures = []
    count = 0
    if (ROOT / ".git").exists():
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT, check=True, capture_output=True,
        )
        paths = [ROOT / name.decode("utf-8") for name in set(listing.stdout.split(b"\0")) if name]
    else:
        paths = list(ROOT.rglob("*"))
    for path in sorted(paths):
        relative = path.relative_to(ROOT)
        if any(part in IGNORED for part in relative.parts):
            continue
        if path.is_symlink():
            failures.append((relative, "symlink is not a distributable source file"))
            continue
        if not path.is_file():
            continue
        if (any(part in FORBIDDEN_PARTS for part in relative.parts)
                or path.suffix in FORBIDDEN_SUFFIXES
                or (path.name.startswith("private-config") and not path.name.endswith(".example.json"))
                or (path.name.startswith(".env") and path.name != ".env.example")
                or ("cookies" in path.name.lower() and relative not in COOKIE_SOURCE_FILES)):
            failures.append((relative, "runtime or credential file"))
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError:
            failures.append((relative, "unexpected binary file"))
            continue
        count += 1
        if path.suffix == ".py":
            try:
                ast.parse(content, filename=str(relative))
            except SyntaxError:
                failures.append((relative, "invalid Python syntax"))
        for kind, pattern in PATTERNS.items():
            if pattern.search(content):
                failures.append((relative, kind))
    for path, reason in failures:
        print(f"{path}: {reason}")
    print(f"Checked {count} text source files; findings: {len(failures)}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
