from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path


DOCKER_SOCKET = Path(
    os.environ.get("DOCKER_SOCKET_PATH", "/var/run/docker.sock")
)
STATE_ROOT = Path(
    os.environ.get("ONLINE_AUTH_AUTO_REFRESH_STATE_DIR", "/state/auth-refresh")
)
ENABLED = os.environ.get(
    "ONLINE_AUTH_AUTO_REFRESH_ENABLED",
    "true",
).strip().lower() in {"1", "true", "yes", "on"}
SETTLE_SECONDS = max(
    3,
    int(os.environ.get("ONLINE_AUTH_AUTO_REFRESH_SETTLE_SECONDS", "12")),
)
BROWSER_READY_TIMEOUT_SECONDS = max(
    15,
    int(os.environ.get("ONLINE_AUTH_BROWSER_READY_TIMEOUT_SECONDS", "60")),
)
EXPORT_TIMEOUT_SECONDS = max(
    60,
    int(os.environ.get("ONLINE_AUTH_EXPORT_TIMEOUT_SECONDS", "240")),
)
SUCCESS_COOLDOWN_SECONDS = max(
    30,
    int(os.environ.get("ONLINE_AUTH_SUCCESS_COOLDOWN_SECONDS", "300")),
)
FAILURE_COOLDOWN_SECONDS = max(
    60,
    int(os.environ.get("ONLINE_AUTH_FAILURE_COOLDOWN_SECONDS", "1800")),
)


@dataclass(frozen=True)
class SiteConfig:
    label: str
    container: str
    exporter: str
    disable_env: str
    manual_hint: str


@dataclass(frozen=True)
class RefreshResult:
    attempted: bool
    success: bool
    detail: str


SITE_CONFIGS = {
    "youtube": SiteConfig(
        label="YouTube",
        container=os.environ.get(
            "YT_AUTH_BROWSER_CONTAINER_NAME",
            "youtube-auth-browser",
        ),
        exporter="/app/nas_youtube_auth.py",
        disable_env="YT_AUTH_AUTO_REFRESH_DISABLED",
        manual_hint=(
            "请在 Mac 运行 open-nas-youtube-auth.sh，完成登录后再运行 "
            "finalize-nas-youtube-auth.sh。"
        ),
    ),
    "bilibili": SiteConfig(
        label="哔哩哔哩",
        container=os.environ.get(
            "BILI_AUTH_BROWSER_CONTAINER_NAME",
            "bilibili-auth-browser",
        ),
        exporter="/app/nas_bilibili_auth.py",
        disable_env="BILI_AUTH_AUTO_REFRESH_DISABLED",
        manual_hint=(
            "请在 Mac 运行 open-nas-bilibili-auth.sh，完成登录后再运行 "
            "finalize-nas-bilibili-auth.sh。"
        ),
    ),
}
_LOCKS = {name: threading.Lock() for name in SITE_CONFIGS}


class DockerSocketConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(DOCKER_SOCKET))


def _docker_request(method: str, path: str) -> tuple[int, bytes]:
    if not DOCKER_SOCKET.exists():
        raise RuntimeError("Docker socket 不存在")
    connection = DockerSocketConnection("localhost", timeout=10)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _container_state(container_name: str) -> dict[str, object] | None:
    name = urllib.parse.quote(container_name, safe="")
    status, payload = _docker_request("GET", f"/containers/{name}/json")
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"Docker API 返回 HTTP {status}")
    return dict(json.loads(payload).get("State") or {})


def _container_action(container_name: str, action: str) -> None:
    name = urllib.parse.quote(container_name, safe="")
    suffix = "?t=20" if action == "stop" else ""
    status, payload = _docker_request(
        "POST",
        f"/containers/{name}/{action}{suffix}",
    )
    accepted = {204, 304}
    if status not in accepted:
        detail = payload.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Docker 无法{action} {container_name}：HTTP {status} {detail}"
        )


def _wait_container_state(
    container_name: str,
    running: bool,
    timeout_seconds: int = 45,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = _container_state(container_name)
        if state is not None and bool(state.get("Running")) is running:
            return
        time.sleep(1)
    expected = "运行" if running else "停止"
    raise RuntimeError(f"{container_name} 未能在限时内进入{expected}状态")


def _wait_browser_port(container_name: str) -> None:
    deadline = time.monotonic() + BROWSER_READY_TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((container_name, 3001), timeout=3):
                return
        except OSError as exc:
            last_error = str(exc)
            time.sleep(2)
    raise RuntimeError(
        f"{container_name} 的浏览器服务未就绪：{last_error or '连接超时'}"
    )


def _state_path(site: str) -> Path:
    return STATE_ROOT / f"{site}.json"


def _load_state(site: str) -> dict[str, object]:
    path = _state_path(site)
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return {}


def _save_state(site: str, success: bool, detail: str) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    path = _state_path(site)
    candidate = path.with_suffix(".json.tmp")
    candidate.write_text(
        json.dumps(
            {
                "last_attempt": time.time(),
                "success": success,
                "detail": detail,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(candidate, path)


def _recent_result(site: str) -> RefreshResult | None:
    state = _load_state(site)
    try:
        age = max(0, time.time() - float(state["last_attempt"]))
    except (KeyError, TypeError, ValueError):
        return None
    previous_success = bool(state.get("success"))
    cooldown = (
        SUCCESS_COOLDOWN_SECONDS
        if previous_success
        else FAILURE_COOLDOWN_SECONDS
    )
    if age >= cooldown:
        return None
    minutes = max(1, int((cooldown - age + 59) // 60))
    if previous_success:
        return RefreshResult(
            attempted=False,
            success=True,
            detail=(
                f"{SITE_CONFIGS[site].label} Cookie 刚刚已由另一项任务自动刷新，"
                "直接使用新会话重试。"
            ),
        )
    return RefreshResult(
        attempted=False,
        success=False,
        detail=(
            f"{SITE_CONFIGS[site].label} 自动刷新刚刚失败，为避免反复触发风控，"
            f"{minutes} 分钟内不再自动尝试。"
            f"{SITE_CONFIGS[site].manual_hint}"
        ),
    )


def _short_output(completed: subprocess.CompletedProcess[str]) -> str:
    combined = "\n".join(
        part.strip()
        for part in (completed.stdout, completed.stderr)
        if part and part.strip()
    )
    if not combined:
        return f"导出程序退出码 {completed.returncode}"
    return combined[-1600:]


def refresh_auth(
    site: str,
    probe_url: str,
    *,
    force: bool = False,
) -> RefreshResult:
    if site not in SITE_CONFIGS:
        raise ValueError(f"unsupported auth site: {site}")
    config = SITE_CONFIGS[site]
    if not ENABLED:
        return RefreshResult(
            attempted=False,
            success=False,
            detail=f"{config.label} Cookie 自动刷新未启用。{config.manual_hint}",
        )
    if os.environ.get(config.disable_env, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return RefreshResult(
            attempted=False,
            success=False,
            detail=f"{config.label} Cookie 自动刷新已在本次校验中禁用。",
        )

    with _LOCKS[site]:
        if not force:
            recent = _recent_result(site)
            if recent is not None:
                return recent

        started_here = False
        try:
            state = _container_state(config.container)
            if state is None:
                raise RuntimeError(
                    f"找不到 {config.container} 容器；请先运行一次登录脚本创建它"
                )
            if bool(state.get("Running")):
                detail = (
                    f"{config.label} 登录浏览器当前正在运行，自动程序没有接管，"
                    f"以免中断人工登录。{config.manual_hint}"
                )
                _save_state(site, False, detail)
                return RefreshResult(False, False, detail)

            print(
                f"[auth-refresh] starting {config.container} for {config.label}",
                flush=True,
            )
            _container_action(config.container, "start")
            started_here = True
            _wait_container_state(config.container, True)
            _wait_browser_port(config.container)
            time.sleep(SETTLE_SECONDS)
        except Exception as exc:
            detail = (
                f"{config.label} 自动启动登录会话失败：{exc}。"
                f"{config.manual_hint}"
            )
            _save_state(site, False, detail)
            return RefreshResult(True, False, detail)
        finally:
            if started_here:
                try:
                    _container_action(config.container, "stop")
                    _wait_container_state(config.container, False)
                except Exception as stop_exc:
                    print(
                        f"[auth-refresh] failed to stop {config.container}: "
                        f"{stop_exc}",
                        flush=True,
                    )

        environment = os.environ.copy()
        environment[config.disable_env] = "1"
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    config.exporter,
                    "--probe-url",
                    probe_url,
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=EXPORT_TIMEOUT_SECONDS,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            detail = (
                f"{config.label} 自动导出 Cookie 超过 "
                f"{EXPORT_TIMEOUT_SECONDS} 秒。{config.manual_hint}"
            )
            _save_state(site, False, detail)
            return RefreshResult(True, False, detail)

        output = _short_output(completed)
        if completed.returncode != 0:
            detail = (
                f"{config.label} 持久化浏览器未能自动恢复有效登录，"
                "通常表示账号已退出、需要二维码/验证码或二次验证。"
                f"校验结果：{output}。{config.manual_hint}"
            )
            _save_state(site, False, detail)
            return RefreshResult(True, False, detail)

        detail = (
            f"{config.label} Cookie 已从 NAS 持久化浏览器自动刷新，"
            "并通过当前链接实测校验。"
        )
        _save_state(site, True, detail)
        print(f"[auth-refresh] {detail} {output}", flush=True)
        return RefreshResult(True, True, detail)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="启动 NAS 持久化浏览器、刷新 Cookie 并实测链接。",
    )
    parser.add_argument("site", choices=sorted(SITE_CONFIGS))
    parser.add_argument("url")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = refresh_auth(args.site, args.url, force=args.force)
    print(result.detail)
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
