#!/usr/bin/env python3
"""Read-only LXServer readiness and authenticated persistent-queue check.

No song search, media download, configuration mutation, or queue mutation is
performed. Credentials and response bodies are never printed.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


TERMINAL_STATUSES = frozenset({"finished", "exists", "error", "cancelled", "canceled"})


class CheckError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CheckError("LXServer redirected the request; use its direct internal address")


def read_auth(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise CheckError("authentication file is unavailable") from None
    if not value or any(char in value for char in "\r\n"):
        raise CheckError("authentication file must contain one nonempty line")
    return value


def check(base_url: str, auth: str, *, timeout: float = 15) -> dict[str, object]:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CheckError("base URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CheckError("base URL must not contain credentials, a query, or a fragment")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def get(path: str):
        request = urllib.request.Request(
            base_url.rstrip("/") + path,
            headers={"accept": "application/json", "x-user-name": "default"},
        )
        request.add_unredirected_header("x-frontend-auth", auth)
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise CheckError(f"{path}: HTTP {code}") from None
        except CheckError:
            raise
        except (OSError, ValueError, UnicodeError):
            raise CheckError(f"{path}: request or JSON response failed") from None

    # /api/music/config and public queue GET can succeed without the admin
    # secret. /api/config checks x-frontend-auth against frontend.password.
    config = get("/api/config")
    if not isinstance(config, dict):
        raise CheckError("LXServer returned an invalid configuration response")
    queue = get("/api/music/cache/queue")
    if not isinstance(queue, dict) or queue.get("success") is not True:
        raise CheckError("LXServer returned an invalid or unsuccessful queue response")
    tasks = queue.get("data")
    if not isinstance(tasks, list) or any(not isinstance(task, dict) for task in tasks):
        raise CheckError("LXServer returned an invalid queue task list")
    # Return counts only: names, paths, URLs, source scripts, and queue payloads
    # can contain private library information.
    statuses = Counter(str(task.get("status") or "unknown") for task in tasks)
    busy = sum(count for status, count in statuses.items() if status not in TERMINAL_STATUSES)
    return {"api": "ready", "queue_tasks": len(tasks), "busy_tasks": busy}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:19527")
    parser.add_argument("--auth-file", type=Path, default=Path("secrets/lx-frontend-auth"))
    parser.add_argument("--require-idle", action="store_true")
    args = parser.parse_args()
    try:
        result = check(args.base_url, read_auth(args.auth_file))
        print(json.dumps(result))
        if args.require_idle and result["busy_tasks"]:
            print("LXServer has active or pending tasks; do not upgrade yet")
            return 2
        return 0
    except CheckError as exc:
        print(f"LXServer check failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
