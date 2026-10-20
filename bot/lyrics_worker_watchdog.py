#!/usr/bin/env python3
"""Keep the persistent word-lyrics workers available without fighting one-off jobs.

One-off repair containers deliberately stop a persistent worker and inherit its
volumes with ``--volumes-from``.  Docker's ``unless-stopped`` policy treats that
as a manual stop, so it will not bring the worker back if the wrapper process is
killed before its EXIT trap runs.  This watchdog fills that gap.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any


DOCKER_SOCKET = os.environ.get("LYRICS_WATCHDOG_DOCKER_SOCKET", "/var/run/docker.sock")
WORKERS = tuple(
    item.strip()
    for item in os.environ.get(
        "LYRICS_WATCHDOG_WORKERS",
        ",".join(
            (
                "apple-music-word-lyrics-aligner",
                "apple-music-word-lyrics-aligner-2",
                "apple-music-word-lyrics-aligner-3",
                "apple-music-word-lyrics-aligner-4",
            )
        ),
    ).split(",")
    if item.strip()
)
ONEOFF_PREFIX = os.environ.get(
    "LYRICS_WATCHDOG_ONEOFF_PREFIX", "word-lyrics-oneoff-"
).strip()
PAUSE_FILE = os.environ.get(
    "LYRICS_WATCHDOG_PAUSE_FILE", "/state/word-lyrics/watchdog.pause"
)
INTERVAL = max(10, int(os.environ.get("LYRICS_WATCHDOG_INTERVAL_SECONDS", "30")))
START_GRACE = max(
    0, int(os.environ.get("LYRICS_WATCHDOG_START_GRACE_SECONDS", "60"))
)
FAILURES_BEFORE_ACTION = max(
    1, int(os.environ.get("LYRICS_WATCHDOG_FAILURES_BEFORE_ACTION", "2"))
)
COOLDOWN = max(
    30, int(os.environ.get("LYRICS_WATCHDOG_COOLDOWN_SECONDS", "300"))
)
REQUEST_TIMEOUT = max(
    5, int(os.environ.get("LYRICS_WATCHDOG_REQUEST_TIMEOUT_SECONDS", "20"))
)
RUN_ONCE = os.environ.get("LYRICS_WATCHDOG_RUN_ONCE", "").lower() in {
    "1",
    "true",
    "yes",
}
DRY_RUN = os.environ.get("LYRICS_WATCHDOG_DRY_RUN", "").lower() in {
    "1",
    "true",
    "yes",
}
HEARTBEAT_FILE = os.environ.get(
    "LYRICS_WATCHDOG_HEARTBEAT_FILE", "/tmp/lyrics-watchdog-heartbeat"
)


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{stamp} lyrics-watchdog: {message}", flush=True)


def decode_chunked(body: bytes) -> bytes:
    decoded = bytearray()
    cursor = 0
    while True:
        line_end = body.find(b"\r\n", cursor)
        if line_end < 0:
            raise RuntimeError("invalid chunked Docker response")
        size_text = body[cursor:line_end].split(b";", 1)[0]
        size = int(size_text, 16)
        cursor = line_end + 2
        if size == 0:
            return bytes(decoded)
        decoded.extend(body[cursor : cursor + size])
        cursor += size + 2


class DockerAPI:
    def request(self, method: str, path: str) -> tuple[int, bytes]:
        request = (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: docker\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        response = bytearray()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(REQUEST_TIMEOUT)
            client.connect(DOCKER_SOCKET)
            client.sendall(request)
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > 16 * 1024 * 1024:
                    raise RuntimeError("Docker response exceeded safety limit")

        header_blob, separator, body = bytes(response).partition(b"\r\n\r\n")
        if not separator:
            raise RuntimeError("invalid Docker HTTP response")
        header_lines = header_blob.split(b"\r\n")
        status_parts = header_lines[0].decode("ascii", errors="replace").split()
        if len(status_parts) < 2 or not status_parts[1].isdigit():
            raise RuntimeError("invalid Docker HTTP status")
        headers: dict[str, str] = {}
        for line in header_lines[1:]:
            key, found, value = line.partition(b":")
            if found:
                headers[key.decode("ascii").lower()] = value.decode(
                    "ascii", errors="replace"
                ).strip()
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body = decode_chunked(body)
        return int(status_parts[1]), body

    def get_json(self, path: str) -> Any:
        status, body = self.request("GET", path)
        if status != 200:
            raise RuntimeError(f"Docker GET {path} returned HTTP {status}")
        return json.loads(body.decode("utf-8"))

    def inspect(self, name_or_id: str) -> dict[str, Any] | None:
        encoded = urllib.parse.quote(name_or_id, safe="")
        status, body = self.request("GET", f"/containers/{encoded}/json")
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"Docker inspect returned HTTP {status}")
        return json.loads(body.decode("utf-8"))

    def start(self, name: str) -> int:
        encoded = urllib.parse.quote(name, safe="")
        status, _ = self.request("POST", f"/containers/{encoded}/start")
        return status

    def restart(self, name: str) -> int:
        encoded = urllib.parse.quote(name, safe="")
        status, _ = self.request("POST", f"/containers/{encoded}/restart?t=30")
        return status


@dataclass(frozen=True)
class WorkerState:
    running: bool
    status: str
    health: str


def worker_state(details: dict[str, Any]) -> WorkerState:
    state = details.get("State") or {}
    health = (state.get("Health") or {}).get("Status") or "none"
    return WorkerState(
        running=bool(state.get("Running")),
        status=str(state.get("Status") or "unknown"),
        health=str(health),
    )


def active_reservations(docker: DockerAPI) -> dict[str, str]:
    """Return persistent-worker -> active one-off container."""
    reservations: dict[str, str] = {}
    for summary in docker.get_json("/containers/json"):
        names = [str(name).lstrip("/") for name in summary.get("Names") or []]
        oneoff_name = next(
            (name for name in names if name.startswith(ONEOFF_PREFIX)), None
        )
        if not oneoff_name:
            continue
        details = docker.inspect(str(summary.get("Id") or oneoff_name))
        if not details or not (details.get("State") or {}).get("Running"):
            continue
        for source in (details.get("HostConfig") or {}).get("VolumesFrom") or []:
            worker = str(source).split(":", 1)[0].lstrip("/")
            if worker in WORKERS:
                reservations[worker] = oneoff_name
    return reservations


def touch_heartbeat() -> None:
    with open(HEARTBEAT_FILE, "a", encoding="utf-8"):
        os.utime(HEARTBEAT_FILE, None)


def main() -> None:
    if not WORKERS:
        raise SystemExit("LYRICS_WATCHDOG_WORKERS is empty")
    if not ONEOFF_PREFIX:
        raise SystemExit("LYRICS_WATCHDOG_ONEOFF_PREFIX is empty")

    docker = DockerAPI()
    failures = {worker: 0 for worker in WORKERS}
    last_action = {worker: 0.0 for worker in WORKERS}
    last_observation: dict[str, str] = {}

    log(
        f"monitoring {len(WORKERS)} workers; one-off prefix={ONEOFF_PREFIX!r}; "
        f"startup grace={START_GRACE}s"
    )
    touch_heartbeat()
    if START_GRACE and not RUN_ONCE:
        time.sleep(START_GRACE)

    while True:
        touch_heartbeat()
        paused = bool(PAUSE_FILE and os.path.exists(PAUSE_FILE))
        try:
            reservations = active_reservations(docker)
        except Exception as exc:
            # Fail closed: without a trustworthy reservation list, starting a
            # stopped worker could compete with a resource-intensive one-off.
            log(f"reservation scan failed ({type(exc).__name__}); no action")
            if RUN_ONCE:
                return
            time.sleep(INTERVAL)
            continue

        for worker in WORKERS:
            try:
                details = docker.inspect(worker)
            except Exception as exc:
                log(f"inspect failed for {worker} ({type(exc).__name__})")
                continue

            if details is None:
                observation = "missing"
                failures[worker] = 0
            else:
                state = worker_state(details)
                reservation = reservations.get(worker)
                if paused:
                    observation = "maintenance-paused"
                    failures[worker] = 0
                elif reservation and not state.running:
                    observation = f"reserved-by:{reservation}"
                    failures[worker] = 0
                elif state.running and state.health != "unhealthy":
                    observation = f"healthy:{state.status}:{state.health}"
                    failures[worker] = 0
                else:
                    action = "restart" if state.running else "start"
                    observation = f"needs-{action}:{state.status}:{state.health}"
                    failures[worker] += 1
                    now = time.monotonic()
                    if (
                        failures[worker] >= FAILURES_BEFORE_ACTION
                        and now - last_action[worker] >= COOLDOWN
                    ):
                        if DRY_RUN:
                            log(f"dry-run: would {action} {worker}")
                            failures[worker] = 0
                            continue
                        try:
                            status = (
                                docker.restart(worker)
                                if action == "restart"
                                else docker.start(worker)
                            )
                        except Exception as exc:
                            log(
                                f"{action} failed for {worker} "
                                f"({type(exc).__name__})"
                            )
                        else:
                            log(
                                f"{action} requested for {worker}; "
                                f"Docker HTTP {status}"
                            )
                            if status in {204, 304}:
                                last_action[worker] = now
                                failures[worker] = 0

            if last_observation.get(worker) != observation:
                log(f"{worker}: {observation}")
                last_observation[worker] = observation

        if RUN_ONCE:
            touch_heartbeat()
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
