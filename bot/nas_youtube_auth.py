#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import http.cookiejar
import json
import os
import socket
import sys
import tempfile
import urllib.parse
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.cookies import load_cookies


DEFAULT_PROFILE = Path(
    os.environ.get(
        "YT_AUTH_BROWSER_PROFILE_PATH",
        "/youtube-auth-browser/.config/chromium/Default",
    )
)
DEFAULT_DESTINATION = Path(
    os.environ.get("YT_COOKIES_PATH", "/state/youtube/cookies.txt")
)
AUTH_BROWSER_CONTAINER = os.environ.get(
    "YT_AUTH_BROWSER_CONTAINER_NAME",
    "youtube-auth-browser",
)
DOCKER_SOCKET = Path("/var/run/docker.sock")
REQUIRED_NAMES = {
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "LOGIN_INFO",
    "__Secure-1PSID",
    "__Secure-3PSID",
}


class DockerSocketConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(DOCKER_SOCKET))


def auth_browser_running() -> bool:
    if not DOCKER_SOCKET.exists():
        return False
    connection = DockerSocketConnection("localhost", timeout=5)
    container = urllib.parse.quote(AUTH_BROWSER_CONTAINER, safe="")
    try:
        connection.request("GET", f"/containers/{container}/json")
        response = connection.getresponse()
        payload = response.read()
        if response.status == 404:
            return False
        if response.status != 200:
            raise RuntimeError(f"Docker API 返回 HTTP {response.status}")
        return bool(json.loads(payload).get("State", {}).get("Running"))
    finally:
        connection.close()


def cookie_names(path: Path) -> set[str]:
    names: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 7 and fields[5]:
            names.add(fields[5])
    return names


def probe_candidate(candidate: Path, url: str) -> object:
    os.environ["YT_COOKIES_PATH"] = str(candidate)
    os.environ["YT_AUTH_AUTO_REFRESH_DISABLED"] = "1"
    from youtube_ingest import PO_TOKEN_ENABLED, probe_url

    strategy = "cookies-po-token" if PO_TOKEN_ENABLED else "cookies-standard"
    return probe_url(
        url,
        preferred_auth_strategy=strategy,
        allow_auth_fallback=False,
    )


def export_profile(
    profile: Path,
    destination: Path,
    probe_url: str = "",
) -> tuple[set[str], object | None]:
    if not profile.is_dir():
        raise RuntimeError(f"NAS Chromium 配置不存在：{profile}")
    if auth_browser_running():
        raise RuntimeError(
            "NAS YouTube 登录浏览器仍在运行。请先关闭页面并停止容器，"
            "再导出 Cookie，以免导出正在轮换或尚未落盘的会话。"
        )

    with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        source = load_cookies(
            None,
            ("chromium", str(profile), "BASICTEXT", None),
            ydl,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        dir=destination.parent,
        delete=False,
    ) as handle:
        candidate = Path(handle.name)

    try:
        filtered = http.cookiejar.MozillaCookieJar(str(candidate))
        for cookie in source:
            domain = cookie.domain.lstrip(".").casefold()
            if domain == "youtube.com" or domain.endswith(".youtube.com"):
                filtered.set_cookie(cookie)
        filtered.save(ignore_discard=True, ignore_expires=True)
        candidate.chmod(0o600)

        names = cookie_names(candidate)
        missing = REQUIRED_NAMES - names
        if missing:
            raise RuntimeError(
                "NAS 浏览器尚未形成完整 YouTube 登录会话，缺少："
                + ", ".join(sorted(missing))
            )
        probe = probe_candidate(candidate, probe_url) if probe_url else None
        os.replace(candidate, destination)
        destination.chmod(0o600)
        return names, probe
    finally:
        candidate.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从已停止的 NAS Chromium 配置中安全导出 YouTube Cookie。",
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument(
        "--probe-url",
        default="",
        help="使用新 Cookie 强制执行一次解析，成功后才替换现有文件。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        names, probe = export_profile(
            args.profile,
            args.destination,
            args.probe_url,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"OK: 已从 NAS Chromium 导出 {len(names)} 项 youtube.com Cookie；"
        f"保存到 {args.destination}"
    )
    if probe is not None:
        print(
            f"OK: Cookie 实测解析成功；标题：{probe.title}；"
            f"策略：{probe.auth_strategy}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
