from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from contextlib import contextmanager
from typing import Callable
from pathlib import Path


DOCKER_SOCKET = Path(
    os.environ.get("DOCKER_SOCKET_PATH", "/var/run/docker.sock")
)
STATE_ROOT = Path(
    os.environ.get("ONLINE_AUTH_AUTO_REFRESH_STATE_DIR", "/state/auth-refresh")
)
ENABLED = os.environ.get(
    "ONLINE_AUTH_AUTO_REFRESH_ENABLED",
    "false",
).strip().lower() in {"1", "true", "yes", "on"}
SETTLE_SECONDS = max(
    3,
    int(os.environ.get("ONLINE_AUTH_AUTO_REFRESH_SETTLE_SECONDS", "12")),
)
YOUTUBE_SETTLE_SECONDS = max(
    SETTLE_SECONDS, int(os.environ.get("YT_AUTH_AUTO_REFRESH_SETTLE_SECONDS", "50")),
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
            "请在部署主机运行 bash services/youtube/youtube-auth.sh start，"
            "在浏览器完成登录后运行该脚本 export <YouTube链接>。"
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


@contextmanager
def _site_lock(site: str, observer: Callable[[], bool] | None):
    acquired = False
    try:
        while not acquired:
            _check_cancelled(observer)
            acquired = _LOCKS[site].acquire(timeout=0.5)
        _check_cancelled(observer)
        yield
    finally:
        if acquired:
            _LOCKS[site].release()


class AuthRefreshCancelled(RuntimeError):
    pass


def _check_cancelled(observer: Callable[[], bool] | None) -> None:
    if observer is not None and observer():
        raise AuthRefreshCancelled("任务已取消或服务正在停止")


def _wait_cancelable(seconds: float, observer: Callable[[], bool] | None) -> None:
    deadline = time.monotonic() + seconds
    while True:
        _check_cancelled(observer)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.5, remaining))


def _run_exporter(
    command: list[str],
    environment: dict[str, str],
    cancel_observer: Callable[[], bool] | None,
) -> subprocess.CompletedProcess[str]:
    _check_cancelled(cancel_observer)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        start_new_session=True,
    )
    deadline = time.monotonic() + EXPORT_TIMEOUT_SECONDS
    try:
        while True:
            _check_cancelled(cancel_observer)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, EXPORT_TIMEOUT_SECONDS)
            try:
                stdout, stderr = process.communicate(timeout=min(0.5, remaining))
                return subprocess.CompletedProcess(
                    command, process.returncode, stdout, stderr,
                )
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        if process.poll() is None:
            # Only this newly spawned exporter process group belongs to us.
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=5)
        raise


def _wait_browser_process(
    container_name: str,
    cancel_observer: Callable[[], bool] | None = None,
) -> None:
    deadline = time.monotonic() + BROWSER_READY_TIMEOUT_SECONDS
    name = urllib.parse.quote(container_name, safe="")
    args = urllib.parse.urlencode({"ps_args": "-eo comm"})
    while time.monotonic() < deadline:
        _check_cancelled(cancel_observer)
        status, payload = _docker_request("GET", f"/containers/{name}/top?{args}")
        if status == 200:
            processes = json.loads(payload).get("Processes") or []
            if any(
                any(
                    ("chromium" in str(value).lower() or str(value).lower() == "chrome")
                    and "crashpad" not in str(value).lower()
                    for value in row
                )
                for row in processes
            ):
                return
        _wait_cancelable(1, cancel_observer)
    raise RuntimeError("登录浏览器界面已就绪，但实际 Chromium 进程未在限时内启动")


def _export_failure_category(output: str) -> str:
    normalized = output.casefold()
    if any(marker in normalized for marker in ("读取超时", "timed out", "timeout", "timeoutexpired")):
        return "candidate_probe_timeout"
    if any(marker in normalized for marker in ("unable to connect to proxy", "connection reset", "connection refused", "remote disconnected", "network is unreachable", "name resolution")):
        return "candidate_probe_network"
    if "完整性校验失败" in output:
        return "candidate_tool_integrity"
    if any(marker in normalized for marker in ("sabr", "requested format is not available", "no video formats found")):
        return "candidate_format_support"
    if "缺少" in output and "cookie" in normalized:
        return "browser_session_incomplete"
    if any(marker in normalized for marker in ("sign in to confirm", "login required", "authentication required", "登录验证", "拒绝了当前")):
        return "candidate_auth_challenge"
    return "cookie_export_or_validation_failed"


def _export_failure_explanation(category: str) -> str:
    return {
        "candidate_probe_timeout": "候选 Cookie 的媒体校验网络超时，未证明浏览器登录失效",
        "candidate_probe_network": "候选 Cookie 的媒体校验遇到代理或网络连接错误，未证明浏览器登录失效",
        "candidate_format_support": "候选 Cookie 的媒体格式校验受限，不能据此判断浏览器已退出登录",
        "candidate_tool_integrity": "专用媒体提取工具完整性校验失败",
        "browser_session_incomplete": "浏览器中缺少必需的登录 Cookie",
        "candidate_auth_challenge": "上游要求登录或人机验证，未证明浏览器已退出登录",
    }.get(category, "Cookie 导出或候选校验未完成，未证明浏览器已退出登录")



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


def _container_action(container_name: str, action: str) -> int:
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
    return status


def _wait_container_state(
    container_name: str,
    running: bool,
    timeout_seconds: int = 45,
    cancel_observer: Callable[[], bool] | None = None,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _check_cancelled(cancel_observer)
        state = _container_state(container_name)
        if state is not None and bool(state.get("Running")) is running:
            return state
        _wait_cancelable(1, cancel_observer)
    expected = "运行" if running else "停止"
    raise RuntimeError(f"{container_name} 未能在限时内进入{expected}状态")


def _wait_browser_port(
    container_name: str,
    cancel_observer: Callable[[], bool] | None = None,
) -> None:
    deadline = time.monotonic() + BROWSER_READY_TIMEOUT_SECONDS
    last_error = ""
    while time.monotonic() < deadline:
        _check_cancelled(cancel_observer)
        try:
            with socket.create_connection((container_name, 3001), timeout=3):
                return
        except OSError as exc:
            last_error = str(exc)
            _wait_cancelable(2, cancel_observer)
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


def _save_state(site: str, success: bool, detail: str, reason_category: str = "") -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    path = _state_path(site)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=f".{site}.",
        dir=STATE_ROOT, delete=False,
    ) as handle:
        candidate = Path(handle.name)
        try:
            json.dump({"last_attempt": time.time(), "success": success, "detail": detail,
                       "reason_category": reason_category or ("success" if success else "unspecified_failure")},
                      handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            candidate.chmod(0o600)
            os.replace(candidate, path)
        finally:
            candidate.unlink(missing_ok=True)


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
    category = str(state.get("reason_category") or "")
    explanation = _export_failure_explanation(category)
    guidance = (
        SITE_CONFIGS[site].manual_hint
        if category in {"browser_session_incomplete", "candidate_auth_challenge"}
        else "现有 Cookie 已保留；稍后可重试校验，无需仅凭此结果重新登录。"
    )
    return RefreshResult(
        attempted=False,
        success=False,
        detail=(
            f"{SITE_CONFIGS[site].label} 自动刷新刚刚失败，为避免反复触发风控，"
            f"{minutes} 分钟内不再自动尝试。"
            f"上次原因：{explanation}。{guidance}"
        ),
    )


def _short_output(completed: subprocess.CompletedProcess[str]) -> str:
    # Browser/exporter errors may contain authenticated URLs or session values.
    # Only the exit status may cross into chat, state files, or shared logs.
    return f"导出程序退出码 {completed.returncode}"


def refresh_auth(
    site: str,
    probe_url: str,
    *,
    force: bool = False,
    cancel_observer: Callable[[], bool] | None = None,
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

    with _site_lock(site, cancel_observer):
        _check_cancelled(cancel_observer)
        if not force:
            recent = _recent_result(site)
            if recent is not None:
                return recent

        started_here = False
        owned_started_at = ""
        ownership_lost = False
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
                return RefreshResult(False, False, detail)

            print(
                f"[auth-refresh] starting {config.container} for {config.label}",
                flush=True,
            )
            start_status = _container_action(config.container, "start")
            if start_status != 204:
                detail = (
                    f"{config.label} 登录浏览器已由其他操作启动，自动程序"
                    "不接管、不停止此人工会话；稍后可再次尝试同步。"
                )
                return RefreshResult(False, False, detail)
            started_here = True
            owned_state = _container_state(config.container) or {}
            owned_started_at = str(owned_state.get("StartedAt") or "")
            if not owned_started_at:
                raise RuntimeError("无法确认本次浏览器启动身份，停止自动接管")
            _wait_container_state(
                config.container, True, cancel_observer=cancel_observer,
            )
            _wait_browser_port(config.container, cancel_observer=cancel_observer)
            if site == "youtube":
                _wait_browser_process(config.container, cancel_observer=cancel_observer)
            _wait_cancelable(
                YOUTUBE_SETTLE_SECONDS if site == "youtube" else SETTLE_SECONDS,
                cancel_observer,
            )
        except AuthRefreshCancelled:
            raise
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
                    current_state = _container_state(config.container) or {}
                    if (
                        owned_started_at
                        and str(current_state.get("StartedAt") or "") == owned_started_at
                    ):
                        _container_action(config.container, "stop")
                        _wait_container_state(config.container, False)
                    else:
                        ownership_lost = True
                        print(
                            "[auth-refresh] browser startup identity changed; "
                            "leaving the newer session untouched",
                            flush=True,
                        )
                except Exception as stop_exc:
                    print(
                        f"[auth-refresh] failed to stop {config.container}: "
                        f"{stop_exc}",
                        flush=True,
                    )

        if ownership_lost:
            return RefreshResult(
                False, False, "浏览器已由其他操作重新启动，保留该会话并停止本次自动导出。",
            )

        environment = os.environ.copy()
        environment[config.disable_env] = "1"
        try:
            completed = _run_exporter(
                [sys.executable, config.exporter, "--probe-url", probe_url],
                environment,
                cancel_observer,
            )
        except AuthRefreshCancelled:
            raise
        except subprocess.TimeoutExpired:
            detail = (
                f"{config.label} 自动导出 Cookie 超过 "
                f"{EXPORT_TIMEOUT_SECONDS} 秒。{config.manual_hint}"
            )
            _save_state(site, False, detail)
            return RefreshResult(True, False, detail)

        _check_cancelled(cancel_observer)
        output = _short_output(completed)
        if completed.returncode != 0:
            category = _export_failure_category("\n".join((completed.stdout or "", completed.stderr or "")))
            explanation = _export_failure_explanation(category)
            hint = config.manual_hint if category in {
                "browser_session_incomplete", "candidate_auth_challenge",
            } else "请稍后重试同画质校验；无需仅凭此结果重新登录。"
            detail = (
                f"{config.label} {explanation}；现有 Cookie 已保留。"
                f"校验结果：{output}。{hint}"
            )
            _save_state(site, False, detail, reason_category=category)
            print("[auth-refresh] " + json.dumps({
                "event": "cookie_refresh_failed", "site": site,
                "reason_category": category, "exit_code": completed.returncode,
                "existing_cookie_preserved": True,
            }, ensure_ascii=False), flush=True)
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
