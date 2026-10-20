#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


HEALTH_URL = os.environ.get("WATCHDOG_HEALTH_URL", "http://wrapper/health")
DOCKER_SOCKET = os.environ.get("WATCHDOG_DOCKER_SOCKET", "/var/run/docker.sock")
CONTAINERS = [
    item.strip()
    for item in os.environ.get(
        "WATCHDOG_CONTAINERS",
        "apple-music-v2-test-legacy-vm,apple-music-v2-test-wrapper-adapter",
    ).split(",")
    if item.strip()
]
INTERVAL = max(15, int(os.environ.get("WATCHDOG_INTERVAL_SECONDS", "30")))
START_GRACE = max(60, int(os.environ.get("WATCHDOG_START_GRACE_SECONDS", "240")))
FAILURES_BEFORE_RESTART = max(
    2,
    int(os.environ.get("WATCHDOG_FAILURES_BEFORE_RESTART", "3")),
)
COOLDOWN = max(180, int(os.environ.get("WATCHDOG_COOLDOWN_SECONDS", "600")))
RESTART_TIMEOUT = max(10, int(os.environ.get("WATCHDOG_RESTART_TIMEOUT_SECONDS", "25")))

DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{stamp} watchdog: {message}", flush=True)


def healthy() -> bool:
    request = urllib.request.Request(
        HEALTH_URL,
        headers={"accept": "application/json", "connection": "close"},
    )
    try:
        with DIRECT_OPENER.open(request, timeout=8) as response:
            payload = json.loads(response.read(65536).decode("utf-8"))
        # The wrapper exposes {"status": "ok", ...}, while the production
        # Bot exposes {"ok": true, "queue": ...}. Both are valid health
        # contracts in this stack.
        reports_healthy = payload.get("status") == "ok" or payload.get("ok") is True
        return response.status == 200 and reports_healthy
    except (OSError, ValueError, urllib.error.URLError):
        return False


def docker_request(method: str, path: str) -> int:
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: docker\r\n"
        "Content-Length: 0\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(RESTART_TIMEOUT + 10)
        client.connect(DOCKER_SOCKET)
        client.sendall(request)
        response = bytearray()
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > 65536:
                break
    status_line = bytes(response).split(b"\r\n", 1)[0].decode(
        "ascii",
        errors="replace",
    )
    parts = status_line.split()
    if len(parts) < 2 or not parts[1].isdigit():
        raise RuntimeError("invalid Docker API response")
    return int(parts[1])


def restart_stack_components() -> None:
    for container in CONTAINERS:
        encoded = urllib.parse.quote(container, safe="")
        path = f"/containers/{encoded}/restart?t={RESTART_TIMEOUT}"
        try:
            status = docker_request("POST", path)
        except Exception as exc:
            log(f"restart failed for {container} ({type(exc).__name__})")
            continue
        log(f"restart requested for {container}; Docker HTTP {status}")


def main() -> None:
    log(f"startup grace {START_GRACE}s")
    time.sleep(START_GRACE)
    failures = 0
    last_restart = 0.0
    while True:
        if healthy():
            if failures:
                log("stack health recovered")
            failures = 0
        else:
            failures += 1
            log(f"health failure {failures}/{FAILURES_BEFORE_RESTART}")
            now = time.monotonic()
            if (
                failures >= FAILURES_BEFORE_RESTART
                and now - last_restart >= COOLDOWN
            ):
                restart_stack_components()
                last_restart = now
                failures = 0
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
