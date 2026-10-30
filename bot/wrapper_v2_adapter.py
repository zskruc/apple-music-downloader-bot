#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import signal
import socket
import socketserver
import struct
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx


VERSION = "0.0.2"
BUILD_VERSION = "0.0.6"
DECRYPT_MAGIC = 0x57563244  # WV2D
DECRYPT_VERSION = 1
DECRYPT_KIND_BATCH = 1
DECRYPT_KIND_OK = 2
DECRYPT_KIND_ERROR = 3
DECRYPT_KIND_CLOSE = 9
MAX_FRAME_BYTES = 300 * 1024 * 1024

SESSION_DIR = Path(os.environ.get("ADAPTER_SESSION_DIR", "/session"))
HTTP_HOST = os.environ.get("ADAPTER_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("ADAPTER_HTTP_PORT", "80"))
DECRYPT_HOST = os.environ.get("ADAPTER_DECRYPT_HOST", "0.0.0.0")
DECRYPT_PORT = int(os.environ.get("ADAPTER_DECRYPT_PORT", "10020"))
LEGACY_HOST = os.environ.get("LEGACY_WRAPPER_HOST", "legacy-wrapper")
LEGACY_DECRYPT_PORT = int(os.environ.get("LEGACY_WRAPPER_DECRYPT_PORT", "10020"))
LEGACY_M3U8_PORT = int(os.environ.get("LEGACY_WRAPPER_M3U8_PORT", "20020"))
LEGACY_ACCOUNT_PORT = int(os.environ.get("LEGACY_WRAPPER_ACCOUNT_PORT", "30020"))
LEGACY_DECRYPT_ATTEMPTS = min(
    3,
    max(1, int(os.environ.get("ADAPTER_LEGACY_DECRYPT_ATTEMPTS", "2"))),
)
LEGACY_M3U8_ATTEMPTS = min(
    10,
    max(1, int(os.environ.get("ADAPTER_LEGACY_M3U8_ATTEMPTS", "3"))),
)
LEGACY_M3U8_RETRY_DELAY_SECONDS = min(
    30.0,
    max(
        0.0,
        float(
            os.environ.get(
                "ADAPTER_LEGACY_M3U8_RETRY_DELAY_SECONDS",
                "1",
            )
        ),
    ),
)
APPLE_LANGUAGE = os.environ.get("APPLE_MUSIC_LANGUAGE", "zh-CN")
TOKEN_REFRESH_SECONDS = max(
    3600,
    int(os.environ.get("ADAPTER_TOKEN_REFRESH_SECONDS", "21600")),
)

APPLE_MUSIC_HOME = "https://music.apple.com"
WEBPLAYBACK_URL = (
    "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/webPlayback"
)

stop_event = threading.Event()
legacy_decrypt_lock = threading.Lock()
legacy_m3u8_lock = threading.Lock()
health_lock = threading.Lock()
health_cache: tuple[float, dict[str, Any]] | None = None


class LegacyM3U8Error(RuntimeError):
    def __init__(self, attempts: int, last_error: Exception) -> None:
        super().__init__(
            f"legacy wrapper M3U8 request failed after {attempts} attempts"
        )
        self.attempts = attempts
        self.last_error_type = type(last_error).__name__


class PlaybackUnavailableError(RuntimeError):
    def __init__(self, failure_type: str, customer_message: str) -> None:
        self.failure_type = failure_type[:64]
        self.customer_message = customer_message[:512]
        super().__init__(
            f"Apple playback is unavailable (failureType={self.failure_type})"
        )


def playback_unavailable_error(
    payload: dict[str, Any],
) -> PlaybackUnavailableError | None:
    failure_type = str(payload.get("failureType") or "").strip()
    dialog = payload.get("dialog")
    dialog_message = (
        str(dialog.get("message") or "").strip()
        if isinstance(dialog, dict)
        else ""
    )
    customer_message = str(payload.get("customerMessage") or "").strip()
    message = customer_message or dialog_message
    if failure_type == "3076" and (
        payload.get("m-allowed") is False or isinstance(dialog, dict)
    ):
        return PlaybackUnavailableError(
            failure_type=failure_type,
            customer_message=message or "Apple Music reports this song is unavailable.",
        )
    return None


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{stamp} adapter: {message}", flush=True)


def read_session_value(*names: str) -> str:
    for name in names:
        try:
            value = (SESSION_DIR / name).read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            continue
        if value and len(value) <= 16384:
            return value
    return ""


def write_session_value(name: str, value: str) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    path = SESSION_DIR / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def fetch_public_developer_token() -> str:
    with httpx.Client(
        trust_env=False,
        follow_redirects=True,
        timeout=httpx.Timeout(30.0),
    ) as client:
        home_response = client.get(APPLE_MUSIC_HOME)
        home_response.raise_for_status()
        index_match = re.search(
            r"/(assets/index[~-][^/\"\s]+\.js)",
            home_response.text,
        )
        if not index_match:
            raise RuntimeError("Apple Music index asset was not found")
        index_response = client.get(
            f"{APPLE_MUSIC_HOME}/{index_match.group(1)}"
        )
        index_response.raise_for_status()
        token_match = re.search(
            r'"(eyJ[A-Za-z0-9\-_]+\.eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+)"',
            index_response.text,
        )
        if not token_match:
            raise RuntimeError("Apple Music developer token was not found")
        return token_match.group(1)


def refresh_developer_token() -> bool:
    try:
        token = fetch_public_developer_token()
        if not token.startswith("eyJ") or token.count(".") != 2:
            raise RuntimeError("unexpected developer token shape")
        write_session_value("DEVELOPER_TOKEN", token)
        log("public developer token refreshed")
        return True
    except Exception as exc:
        log(f"developer token refresh failed ({type(exc).__name__})")
        return False


def token_refresh_loop() -> None:
    while not stop_event.wait(TOKEN_REFRESH_SECONDS):
        refresh_developer_token()


def get_legacy_account() -> dict[str, str]:
    url = f"http://{LEGACY_HOST}:{LEGACY_ACCOUNT_PORT}/"
    try:
        with httpx.Client(trust_env=False, timeout=5.0) as client:
            response = client.get(url)
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, str] = {}
    for key in ("music_token", "dev_token"):
        value = payload.get(key)
        if isinstance(value, str) and value and len(value) <= 16384:
            result[key] = value
    return result


def get_auth_tokens() -> tuple[str, str]:
    legacy = get_legacy_account()
    music_token = legacy.get("music_token") or read_session_value("MUSIC_TOKEN")
    developer_token = (
        read_session_value("DEVELOPER_TOKEN")
        or legacy.get("dev_token")
        or read_session_value("DEV_TOKEN")
    )
    return music_token, developer_token


def port_ready(port: int) -> bool:
    try:
        with socket.create_connection((LEGACY_HOST, port), timeout=2.0):
            return True
    except OSError:
        return False


def health_snapshot(use_cache: bool = True) -> dict[str, Any]:
    global health_cache
    now = time.monotonic()
    with health_lock:
        if use_cache and health_cache and now - health_cache[0] < 5:
            return dict(health_cache[1])

    music_token, developer_token = get_auth_tokens()
    status = {
        "account_ready": port_ready(LEGACY_ACCOUNT_PORT),
        "m3u8_ready": port_ready(LEGACY_M3U8_PORT),
        "decrypt_ready": port_ready(LEGACY_DECRYPT_PORT),
        "music_token_ready": bool(music_token),
        "developer_token_ready": bool(developer_token),
    }
    status["ready"] = all(status.values())
    with health_lock:
        health_cache = (now, dict(status))
    return status


def encode_legacy_m3u8_request(adam_id: str) -> bytes:
    adam_bytes = adam_id.encode("utf-8")
    if not adam_bytes or len(adam_bytes) > 254:
        raise ValueError("invalid Adam ID length")

    # The legacy wrapper treats the length-prefixed Adam ID as a C string but
    # does not add its own terminator. Nine-digit IDs happened to work because
    # the following byte in its buffer was zero; ten-digit IDs consumed that
    # byte and were read with a stray trailing character. Include the NUL in
    # the declared payload so every ID is terminated deterministically.
    payload = adam_bytes + b"\0"
    return bytes([len(payload)]) + payload


def _get_legacy_m3u8_once(adam_id: str) -> str:
    with socket.create_connection(
        (LEGACY_HOST, LEGACY_M3U8_PORT),
        timeout=30,
    ) as client:
        client.settimeout(60)
        client.sendall(encode_legacy_m3u8_request(adam_id))
        stream = client.makefile("rb")
        raw = stream.readline(65536)
    if not raw or len(raw) >= 65536:
        raise RuntimeError("legacy wrapper returned no M3U8 URL")
    url = raw.decode("utf-8", "strict").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError("legacy wrapper returned an invalid M3U8 URL")
    return url


def get_legacy_m3u8(adam_id: str) -> str:
    with legacy_m3u8_lock:
        for attempt in range(1, LEGACY_M3U8_ATTEMPTS + 1):
            try:
                return _get_legacy_m3u8_once(adam_id)
            except (OSError, RuntimeError, UnicodeError) as exc:
                if attempt >= LEGACY_M3U8_ATTEMPTS:
                    raise LegacyM3U8Error(attempt, exc) from exc
                log(
                    "legacy M3U8 request failed "
                    f"({type(exc).__name__}); retrying "
                    f"({attempt + 1}/{LEGACY_M3U8_ATTEMPTS})"
                )
                time.sleep(LEGACY_M3U8_RETRY_DELAY_SECONDS)
    raise RuntimeError("legacy M3U8 retry loop exited unexpectedly")


def get_webplayback(
    adam_id: str,
    music_token: str,
    developer_token: str,
) -> dict[str, Any]:
    headers = {
        "authorization": f"Bearer {developer_token}",
        "origin": APPLE_MUSIC_HOME,
        "cookie": f"media-user-token={music_token}",
        "user-agent": "AppleMusicWrapperV2Adapter/1.0",
    }
    # Retry only this HTTP exchange.  Semantic playback failures and the
    # separately bounded legacy M3U8 operation must not be repeated here.
    for attempt in range(1, 4):
        try:
            with httpx.Client(
                trust_env=False,
                timeout=httpx.Timeout(60.0),
            ) as client:
                response = client.post(
                    WEBPLAYBACK_URL,
                    headers=headers,
                    json={"salableAdamId": adam_id, "language": APPLE_LANGUAGE},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.TransportError as exc:
            if attempt >= 3:
                raise
            error_detail = f"error={type(exc).__name__}"
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in {408, 429, 500, 502, 503, 504} or attempt >= 3:
                raise
            error_detail = f"error=HTTPStatusError status={status}"
        else:
            break
        delay = 2 ** attempt
        log(
            "webplayback temporary HTTP failure "
            f"{error_detail}; retrying attempt={attempt + 1}/3 delay={delay}s"
        )
        time.sleep(delay)
    if not isinstance(payload, dict):
        raise RuntimeError("Apple web playback returned invalid JSON")
    if unavailable := playback_unavailable_error(payload):
        raise unavailable
    if "dialog" in payload:
        raise RuntimeError("Apple web playback returned a dialog")
    return payload


def combined_playback(adam_id: str) -> dict[str, Any]:
    music_token, developer_token = get_auth_tokens()
    if not music_token or not developer_token:
        raise PermissionError("test wrapper account is not authenticated")
    playback = get_webplayback(adam_id, music_token, developer_token)
    song_list = playback.get("songList")
    if not isinstance(song_list, list) or not song_list:
        raise RuntimeError("Apple web playback returned no songs")
    first = song_list[0]
    if not isinstance(first, dict):
        raise RuntimeError("Apple web playback returned an invalid song")
    first["hls-playlist-url"] = get_legacy_m3u8(adam_id)
    return playback


class HttpHandler(BaseHTTPRequestHandler):
    server_version = "AppleMusicWrapperV2Adapter/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            health = health_snapshot()
            self.send_json(
                200 if health["ready"] else 503,
                {
                    "status": "ok" if health["ready"] else "degraded",
                    "version": VERSION,
                    "build_version": BUILD_VERSION,
                    "mode": "legacy-protocol-adapter",
                    "runtime": health,
                },
            )
            return

        if parsed.path == "/me":
            music_token, developer_token = get_auth_tokens()
            health = health_snapshot()
            authenticated = bool(music_token and developer_token)
            auth: dict[str, Any] = {
                "state": "authenticated" if authenticated else "logged_out",
            }
            if authenticated:
                auth.update(
                    {
                        "music_user_token": music_token,
                        "dev_token": developer_token,
                        "storefront": read_session_value("STOREFRONT_ID"),
                    }
                )
            self.send_json(
                200,
                {
                    "version": VERSION,
                    "runtime": {
                        "apple_init_enabled": True,
                        "loader_ok": health["ready"],
                        "initialized": health["ready"],
                        "playback_ready": health["ready"],
                        "backend": "isolated-legacy-wrapper-vm",
                    },
                    "auth": auth,
                },
            )
            return

        if parsed.path == "/playback":
            query = urllib.parse.parse_qs(parsed.query)
            adam_id = (query.get("adam_id") or query.get("adamId") or [""])[0]
            if not adam_id.isdigit() or len(adam_id) > 32:
                self.send_json(
                    400,
                    {"error": "invalid_adam_id"},
                )
                return
            try:
                payload = combined_playback(adam_id)
            except PermissionError:
                self.send_json(401, {"error": "not_authenticated"})
            except PlaybackUnavailableError as exc:
                log(
                    "playback unavailable "
                    f"failure_type={exc.failure_type} adam_id={adam_id}"
                )
                self.send_json(
                    451,
                    {
                        "error": "playback_unavailable",
                        "failureType": exc.failure_type,
                        "customerMessage": exc.customer_message,
                    },
                )
            except httpx.HTTPStatusError as exc:
                self.send_json(
                    502,
                    {
                        "error": "webplayback_http_error",
                        "upstream_status": exc.response.status_code,
                    },
                )
            except LegacyM3U8Error as exc:
                log(
                    "playback M3U8 request failed after "
                    f"{exc.attempts} attempts ({exc.last_error_type})"
                )
                self.send_json(
                    502,
                    {
                        "error": "legacy_m3u8_failed",
                        "detail": (
                            "Wrapper m3u8 request for track failed after "
                            f"{exc.attempts} attempts"
                        ),
                        "attempts": exc.attempts,
                    },
                )
            except Exception as exc:
                log(f"playback request failed ({type(exc).__name__})")
                self.send_json(
                    502,
                    {"error": "playback_adapter_failed"},
                )
            else:
                self.send_json(200, payload)
            return

        self.send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        self.send_json(
            409,
            {
                "error": "login_not_supported",
                "detail": "This isolated adapter restores the copied test session.",
            },
        )

    def do_DELETE(self) -> None:
        self.send_json(405, {"error": "method_not_allowed"})


def read_exact(client: socket.socket, length: int) -> bytes:
    output = bytearray()
    while len(output) < length:
        chunk = client.recv(length - len(output))
        if not chunk:
            raise EOFError("truncated socket payload")
        output.extend(chunk)
    return bytes(output)


def parse_batch_payload(payload: bytes) -> tuple[bytes, bytes, list[bytes]]:
    if len(payload) < 8:
        raise ValueError("batch payload is too short")
    adam_len, uri_len, sample_count = struct.unpack(">HHI", payload[:8])
    if not adam_len or not uri_len or not sample_count:
        raise ValueError("batch payload has empty fields")
    table_end = 8 + sample_count * 4
    if table_end > len(payload):
        raise ValueError("batch length table is truncated")
    lengths = [
        struct.unpack(">I", payload[8 + index * 4 : 12 + index * 4])[0]
        for index in range(sample_count)
    ]
    if any(length <= 0 for length in lengths):
        raise ValueError("batch contains an empty sample")
    labels_end = table_end + adam_len + uri_len
    if labels_end > len(payload):
        raise ValueError("batch labels are truncated")
    adam_id = payload[table_end : table_end + adam_len]
    skd_uri = payload[table_end + adam_len : labels_end]
    samples: list[bytes] = []
    offset = labels_end
    for length in lengths:
        end = offset + length
        if end > len(payload):
            raise ValueError("batch sample is truncated")
        samples.append(payload[offset:end])
        offset = end
    if offset != len(payload):
        raise ValueError("batch contains trailing bytes")
    return adam_id, skd_uri, samples


def _legacy_decrypt_once(
    adam_id: bytes,
    skd_uri: bytes,
    samples: list[bytes],
) -> list[bytes]:
    with socket.create_connection(
        (LEGACY_HOST, LEGACY_DECRYPT_PORT),
        timeout=30,
    ) as backend:
        backend.settimeout(600)
        backend.sendall(
            bytes([len(adam_id)])
            + adam_id
            + bytes([len(skd_uri)])
            + skd_uri
        )
        plaintexts: list[bytes] = []
        for sample in samples:
            backend.sendall(struct.pack("<I", len(sample)) + sample)
            plaintexts.append(read_exact(backend, len(sample)))
        backend.sendall(b"\0\0\0\0\0")
        return plaintexts


def legacy_decrypt(
    adam_id: bytes,
    skd_uri: bytes,
    samples: list[bytes],
) -> list[bytes]:
    if len(adam_id) > 255 or len(skd_uri) > 255:
        raise ValueError("legacy wrapper label length exceeded")
    with legacy_decrypt_lock:
        for attempt in range(1, LEGACY_DECRYPT_ATTEMPTS + 1):
            try:
                return _legacy_decrypt_once(adam_id, skd_uri, samples)
            except (EOFError, OSError) as exc:
                if attempt >= LEGACY_DECRYPT_ATTEMPTS:
                    raise
                log(
                    "legacy decrypt connection failed "
                    f"({type(exc).__name__}); reconnecting "
                    f"({attempt + 1}/{LEGACY_DECRYPT_ATTEMPTS})"
                )
                time.sleep(0.25)
    raise RuntimeError("legacy decrypt attempts exhausted")


def make_response_payload(samples: list[bytes]) -> bytes:
    output = bytearray(struct.pack(">I", len(samples)))
    for sample in samples:
        output.extend(struct.pack(">I", len(sample)))
    for sample in samples:
        output.extend(sample)
    return bytes(output)


def send_frame(
    client: socket.socket,
    kind: int,
    request_id: int,
    payload: bytes,
) -> None:
    header = struct.pack(
        ">IHHII",
        DECRYPT_MAGIC,
        DECRYPT_VERSION,
        kind,
        request_id,
        len(payload),
    )
    client.sendall(header + payload)


class DecryptHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(600)
        while not stop_event.is_set():
            try:
                header = read_exact(client, 16)
            except EOFError:
                return
            magic, version, kind, request_id, payload_len = struct.unpack(
                ">IHHII",
                header,
            )
            if magic != DECRYPT_MAGIC or version != DECRYPT_VERSION:
                return
            if payload_len > MAX_FRAME_BYTES:
                return
            payload = read_exact(client, payload_len)
            if kind == DECRYPT_KIND_CLOSE:
                return
            if kind != DECRYPT_KIND_BATCH:
                send_frame(
                    client,
                    DECRYPT_KIND_ERROR,
                    request_id,
                    b"unsupported decrypt frame",
                )
                return
            try:
                adam_id, skd_uri, samples = parse_batch_payload(payload)
                plaintexts = legacy_decrypt(adam_id, skd_uri, samples)
                response = make_response_payload(plaintexts)
                send_frame(client, DECRYPT_KIND_OK, request_id, response)
            except Exception as exc:
                log(f"decrypt batch failed ({type(exc).__name__})")
                send_frame(
                    client,
                    DECRYPT_KIND_ERROR,
                    request_id,
                    b"legacy decrypt adapter failed",
                )
                return


class DecryptServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    refreshed = refresh_developer_token()
    if not refreshed and not read_session_value("DEVELOPER_TOKEN", "DEV_TOKEN"):
        log("starting without a developer token; health remains degraded")
    threading.Thread(target=token_refresh_loop, daemon=True).start()

    decrypt_server = DecryptServer(
        (DECRYPT_HOST, DECRYPT_PORT),
        DecryptHandler,
    )
    decrypt_thread = threading.Thread(
        target=decrypt_server.serve_forever,
        kwargs={"poll_interval": 0.5},
        daemon=True,
    )
    decrypt_thread.start()

    http_server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), HttpHandler)

    def stop_servers(_signum: int, _frame: object) -> None:
        stop_event.set()
        threading.Thread(target=http_server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_servers)
    signal.signal(signal.SIGINT, stop_servers)
    log(
        f"wrapper-v2 API listening on {HTTP_HOST}:{HTTP_PORT}; "
        f"WV2D listening on {DECRYPT_HOST}:{DECRYPT_PORT}"
    )
    try:
        http_server.serve_forever(poll_interval=0.5)
    finally:
        stop_event.set()
        http_server.server_close()
        decrypt_server.shutdown()
        decrypt_server.server_close()


if __name__ == "__main__":
    main()
