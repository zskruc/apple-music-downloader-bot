#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import http.client
import http.cookiejar
import json
import os
import socket
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

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
DOCKER_SOCKET = Path(os.environ.get("DOCKER_SOCKET_PATH", "/var/run/docker.sock"))
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
        raise RuntimeError(
            "无法检查浏览器状态：Docker socket 未挂载。请通过 "
            "services/youtube/youtube-auth.sh 停止并确认浏览器后导出。"
        )
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
    import youtube_ingest

    # The helper can also be called in an already-imported process. Override the
    # module settings, rather than relying on import-time environment parsing.
    previous_path = youtube_ingest.COOKIE_PATH
    previous_refresh = youtube_ingest.AUTH_AUTO_REFRESH_DISABLED
    youtube_ingest.COOKIE_PATH = candidate
    youtube_ingest.AUTH_AUTO_REFRESH_DISABLED = True
    try:
        strategy = (
            "cookies-sabr"
            if youtube_ingest.PO_TOKEN_ENABLED and youtube_ingest._sabr_tool_path() is not None
            else "cookies-po-token" if youtube_ingest.PO_TOKEN_ENABLED
            else "cookies-standard"
        )
        return youtube_ingest.probe_url(
            url, preferred_auth_strategy=strategy, allow_auth_fallback=False,
        )
    finally:
        youtube_ingest.COOKIE_PATH = previous_path
        youtube_ingest.AUTH_AUTO_REFRESH_DISABLED = previous_refresh


def _check_browser_stopped(browser_stopped: bool) -> None:
    # A mounted socket always wins over the external operator assertion.
    if DOCKER_SOCKET.exists() or not browser_stopped:
        if auth_browser_running():
            raise RuntimeError("YouTube 登录浏览器仍在运行；请先停止容器。")


def _copy_private(source: Path, destination: Path) -> None:
    """Copy through a private temporary file, then atomically publish it."""
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", dir=destination.parent, delete=False,
    ) as handle:
        temporary = Path(handle.name)
        try:
            with source.open("rb") as source_handle:
                while chunk := source_handle.read(65536):
                    handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def _destination_lock(destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = destination.with_name(f".{destination.name}.lock")
    descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise RuntimeError("另一项 Cookie 导出或回滚正在运行，请稍后重试。") from None
    finally:
        os.close(descriptor)


def _validate_candidate(candidate: Path, url: str, skip_probe: bool):
    names = cookie_names(candidate)
    missing = REQUIRED_NAMES - names
    if missing:
        raise RuntimeError("YouTube 登录会话不完整，缺少：" + ", ".join(sorted(missing)))
    if not url and not skip_probe:
        raise RuntimeError("必须提供 --probe-url；离线维护需显式指定 --skip-probe。")
    try:
        probe = probe_candidate(candidate, url) if url else None
    except Exception:
        # Extractor output can contain authenticated request details. Do not
        # propagate it to chat messages, shared reports, or refresh-state files.
        raise RuntimeError(
            "候选 Cookie 实测未完成，保留现有文件；请检查网络和媒体可用性，"
            "确认需要登录时再手动认证。"
        ) from None
    return names, probe


def status(profile: Path, destination: Path) -> dict[str, object]:
    """Report metadata only; never serialize cookie values or browser data."""
    info: dict[str, object] = {
        "profile_exists": profile.is_dir(),
        "cookies_exists": destination.is_file(),
        "previous_exists": destination.with_name(destination.name + ".previous").is_file(),
        "browser_running": None,
    }
    if DOCKER_SOCKET.exists():
        try:
            info["browser_running"] = auth_browser_running()
        except Exception:
            info["browser_check"] = "unavailable"
    if destination.is_file():
        metadata = destination.stat()
        info.update({
            "bytes": metadata.st_size,
            "age_seconds": max(0, int(time.time() - metadata.st_mtime)),
            "mode": f"{metadata.st_mode & 0o777:04o}",
            "missing_cookie_names": sorted(REQUIRED_NAMES - cookie_names(destination)),
        })
    return info


def export_profile(
    profile: Path,
    destination: Path,
    probe_url: str = "",
    *,
    browser_stopped: bool = False,
    skip_probe: bool = False,
) -> tuple[set[str], object | None]:
    if not profile.is_dir():
        raise RuntimeError(f"NAS Chromium 配置不存在：{profile}")
    _check_browser_stopped(browser_stopped)
    if not probe_url and not skip_probe:
        raise RuntimeError("必须提供 --probe-url；离线维护需显式指定 --skip-probe。")
    with _destination_lock(destination):
        return _export_profile(profile, destination, probe_url, skip_probe)


def _export_profile(profile: Path, destination: Path, probe_url: str, skip_probe: bool):
    from yt_dlp import YoutubeDL
    from yt_dlp.cookies import load_cookies

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

        names, probe = _validate_candidate(candidate, probe_url, skip_probe)
        if destination.is_file():
            _copy_private(destination, destination.with_name(destination.name + ".previous"))
        os.replace(candidate, destination)
        destination.chmod(0o600)
        return names, probe
    finally:
        candidate.unlink(missing_ok=True)


def rollback(destination: Path, probe_url: str, *, browser_stopped: bool = False,
             skip_probe: bool = False) -> tuple[set[str], object | None]:
    _check_browser_stopped(browser_stopped)
    previous = destination.with_name(destination.name + ".previous")
    with _destination_lock(destination):
        if not previous.is_file():
            raise RuntimeError("没有可回滚的 .previous Cookie 文件。")
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.rollback.", dir=destination.parent, delete=False,
        ) as handle:
            candidate = Path(handle.name)
        try:
            _copy_private(previous, candidate)
            names, probe = _validate_candidate(candidate, probe_url, skip_probe)
            if destination.is_file():
                _copy_private(destination, previous)
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
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true", help="只读输出状态元数据。")
    actions.add_argument("--rollback", action="store_true", help="校验并恢复 .previous 文件。")
    parser.add_argument("--browser-stopped", action="store_true",
                        help="仅无 Docker socket 时，确认已在主机停止浏览器。")
    parser.add_argument("--skip-probe", action="store_true",
                        help="显式离线维护；不会证明 Cookie 可用于实际下载。")
    parser.add_argument(
        "--probe-url",
        default="",
        help="使用新 Cookie 强制执行一次解析，成功后才替换现有文件。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.status:
            print(json.dumps(status(args.profile, args.destination), ensure_ascii=False, indent=2))
            return 0
        if args.rollback:
            names, probe = rollback(args.destination, args.probe_url,
                                    browser_stopped=args.browser_stopped, skip_probe=args.skip_probe)
        else:
            names, probe = export_profile(args.profile, args.destination, args.probe_url,
                                          browser_stopped=args.browser_stopped, skip_probe=args.skip_probe)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"OK: 已{'回滚' if args.rollback else '导出'} {len(names)} 项 youtube.com Cookie；"
        f"保存到 {args.destination}"
    )
    if probe is not None:
        print(
            "OK: Cookie 实测解析成功。"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
