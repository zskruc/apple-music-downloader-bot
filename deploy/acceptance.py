"""Real Docker/KVM install, offline snapshot and restore acceptance harness.

Run only on a disposable Linux x86_64 KVM host. This module never mocks Docker,
logs into music accounts, submits media jobs, or calls Telegram getUpdates.
Only its sanitized report is suitable for a CI artifact; the bundle and logs
remain private. Example:
  python3 -m deploy.acceptance --root /local/amdl-acceptance --report /local/report.json
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse

from deploy.installer import OWNER_LABEL, RESTORE_TASK_WRITERS, SOURCE, STAGES, atomic_json, read_env, read_json, validate_root
from deploy import manage


class AcceptanceError(RuntimeError):
    """A fixed, credential-free failure category; raw diagnostics stay private."""


STAGE_DIAGNOSTIC_LOGS = {
    "configuration": ("compose-validation",),
    "credentials": ("sq-private-prepare",),
    "images": ("restore-image-archive", "verify-restored-image"),
    "dependencies": ("sq-dependency-build",),
    "build": ("core-build",),
    "pull": ("fixed-images",),
    "containers": ("create-stopped-services",),
    "services": ("start-core-services",),
    "sq_configuration": ("sq-schema-configure", "reload-sq-config"),
    "activation": ("activate-restored-queue-services", "activate-bot-and-watchdogs", "hold-restored-task-writers", "hold-unconfigured-bot"),
}

FATAL_READINESS_ERRORS = frozenset({
    "empty_credentials_bot_was_started",
    "restored_task_writer_started_before_cutover",
    "service_mount_leaves_new_installation_root",
    "restored_mount_still_points_to_old_root",
    "guest_ssh_port_not_owned_by_test_vm",
})


def credential_values(values):
    """Select credentials, never auth retry counts, flags, paths or image tags."""
    result = []
    for key, value in values.items():
        credential = re.search(r"(?:^|_)(?:PASSWORD|TOKEN|SECRET|API_KEY)$", key) or key.endswith("_WEBHOOK_URL")
        proxy_url = (key.endswith("_PROXY_URL") or key.endswith("_PROXY")) and "://" in value
        if value and (credential or proxy_url):
            result.append(value)
    return result


def installer_progress(root):
    """Return fixed stage names only; ignore arbitrary values in private state."""
    try:
        state = read_json(root / "state/install.json")
        if not state:
            return "preparing"
        if state.get("status") == "installed":
            return "installed"
        failed = state.get("failed_stage")
        if failed in STAGES:
            return "failed at " + failed
        completed = state.get("completed", [])
        if not isinstance(completed, list):
            return "preparing"
        stage = next((stage for stage in STAGES if stage not in completed), None)
        return stage if stage else "finishing"
    except (OSError, ValueError, TypeError, AttributeError):
        return "preparing"


@contextmanager
def installation_heartbeat(root):
    if root is None:
        yield
        return
    stopped = threading.Event()
    name = root.name if root.name in {"original", "restored"} else "installation"
    def emit():
        print("Installer progress: " + name + " " + installer_progress(root), flush=True)
    def heartbeat():
        while not stopped.is_set():
            emit()
            if stopped.wait(5):
                break
    worker = threading.Thread(target=heartbeat, name="acceptance-installer-progress", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=1)
        emit()


def safe_failure(error):
    text = str(error)
    if isinstance(error, AcceptanceError) and re.fullmatch(r"[a-z0-9_]{1,150}", text):
        return text
    return "unexpected_" + re.sub(r"[^a-z0-9_]", "_", type(error).__name__.lower())


def redact_diagnostic(line, private_values, workspace, source):
    for value in sorted(set(private_values), key=len, reverse=True):
        if value:
            line = line.replace(value, "<redacted>")
    line = line.replace(str(workspace), "<workspace>").replace(str(source), "<source>")
    def url(match):
        try:
            parsed = urllib.parse.urlsplit(match.group())
            host = parsed.hostname or "<host>"
            if parsed.port:
                host += ":" + str(parsed.port)
            return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "<redacted>" if parsed.query else "", ""))
        except ValueError:
            return "<redacted-url>"
    return re.sub(r"https?://[^\s'\"<>]+", url, line)[:500]


def new_workspace(path):
    path = validate_root(path)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise AcceptanceError("acceptance_root_must_be_new_or_empty")
    return path


def clean_environment(source):
    private_keys = set(read_env(source / ".env.example"))
    private_keys.update({"START_BOT", "PRIVATE_ENV_FILE", "INSTALL_MIN_FREE_GIB", "INSTALL_MIN_AVAILABLE_GIB"})
    return {key: value for key, value in os.environ.items()
            if key not in private_keys and not key.startswith(("COMPOSE_", "TELEGRAM_", "SYNOLOGY_", "AMDL_", "YT_", "BILI_", "SQ_", "LX_", "WORD_LYRICS_"))}


@contextmanager
def isolated_environment(source):
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(clean_environment_from(previous, source))
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def clean_environment_from(environment, source):
    keys = set(read_env(source / ".env.example")) | {"START_BOT", "PRIVATE_ENV_FILE", "INSTALL_MIN_FREE_GIB", "INSTALL_MIN_AVAILABLE_GIB"}
    return {key: value for key, value in environment.items()
            if key not in keys and not key.startswith(("COMPOSE_", "TELEGRAM_", "SYNOLOGY_", "AMDL_", "YT_", "BILI_", "SQ_", "LX_", "WORD_LYRICS_"))}


def allocate_ports():
    keys = ["BOT_HEALTH_PORT", "VM_MANAGER_PORT", "VM_SSH_HOST_PORT", "YOUTUBE_BROWSER_PORT",
            "BILIBILI_BROWSER_PORT", "LX_HOST_PORT", "SQ_HOST_PORT", "NAVIDROME_HOST_PORT", "EMBY_HOST_PORT", "LRCAPI_HOST_PORT"]
    listeners = []
    try:
        for _key in keys:
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listeners.append(listener)
        return {key: str(listener.getsockname()[1]) for key, listener in zip(keys, listeners)}
    finally:
        for listener in listeners:
            listener.close()


def source_task_schema(source):
    tree = ast.parse((source / "bot/task_cards.py").read_text(encoding="utf-8"))
    table = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TaskCardStore")
    for node in ast.walk(table):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "executescript" and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                and "CREATE TABLE IF NOT EXISTS task_cards" in node.args[0].value):
            return node.args[0].value
    raise AcceptanceError("source_task_schema_not_found")


class Harness:
    def __init__(self, root, report, include_images=False, timeout=1200, keep_containers=False,
                 min_free_gib=20, source=SOURCE):
        self.root = new_workspace(root)
        self.report_path = Path(report).absolute()
        if self.report_path.is_symlink():
            raise AcceptanceError("report_must_not_be_a_symlink")
        self.original = self.root / "original"
        self.restored = self.root / "restored"
        self.bundle = self.root / "bundle"
        if self.report_path == self.bundle or self.bundle in self.report_path.parents:
            raise AcceptanceError("report_must_be_outside_private_bundle")
        self.source = Path(source).resolve()
        self.include_images = bool(include_images)
        self.timeout = timeout
        self.keep_containers = keep_containers
        self.min_free_gib = min_free_gib
        self.phase = "initialization"
        self.result = {"schema": 1, "kind": "real-docker-kvm-acceptance", "passed": False,
                       "images_in_bundle": self.include_images, "checks": {}, "container_states": {},
                       "not_tested": ["Apple account login/decryption", "YouTube/Bilibili account login and Cookie lifetime",
                                      "real music/video download and highest formats", "subtitle availability, bilingual timing and playback",
                                      "authorized LX/SQ music providers", "Telegram/Synology delivery", "Emby/Navidrome account configuration"]}

    def progress(self, phase):
        self.phase = phase
        print("Acceptance stage: " + phase, flush=True)

    def command(self, arguments, label, *, timeout=180, capture=False, input_text=None, allow_failure=False, progress_root=None):
        log = self.root / "private-logs" / (label + ".log")
        log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            try:
                with installation_heartbeat(progress_root):
                    result = subprocess.run(list(arguments), cwd=str(self.source), input=input_text,
                                            stdout=subprocess.PIPE if capture else output, stderr=subprocess.STDOUT,
                                            text=True, check=False, timeout=timeout, env=clean_environment(self.source))
            except (OSError, subprocess.SubprocessError):
                raise AcceptanceError(label.replace("-", "_") + "_process_failed") from None
            if capture:
                output.write(result.stdout or "")
        if result.returncode and not allow_failure:
            raise AcceptanceError(label.replace("-", "_") + "_exit_" + str(result.returncode))
        return result

    def owned_containers(self, root):
        descriptor = read_json(root / "deployment.json")
        if not descriptor:
            return []
        owner, project = descriptor.get("installation_id"), descriptor.get("project_name")
        if not owner or not project:
            raise AcceptanceError("missing_installation_ownership")
        result = self.command(["docker", "ps", "-a", "--filter", "label=" + OWNER_LABEL + "=" + owner,
                               "--filter", "label=com.docker.compose.project=" + project, "--format", "{{.ID}}"],
                              root.name + "-owned-ids", capture=True)
        rows = []
        for identifier in result.stdout.split():
            # Select individual fields, never Config.Env or full inspect data.
            labels = json.loads(self.command(["docker", "inspect", "--format", "{{json .Config.Labels}}", identifier],
                                             root.name + "-labels", capture=True).stdout)
            if labels.get(OWNER_LABEL) != owner or labels.get("com.docker.compose.project") != project:
                raise AcceptanceError("container_ownership_changed")
            state = json.loads(self.command(["docker", "inspect", "--format", "{{json .State}}", identifier],
                                            root.name + "-state", capture=True).stdout)
            rows.append({"id": identifier, "service": labels.get("com.docker.compose.service"),
                         "state": state.get("Status"), "running": bool(state.get("Running")),
                         "health": state.get("Health", {}).get("Status"), "started_at": state.get("StartedAt", "")})
        return rows

    def save_states(self, root):
        rows = self.owned_containers(root)
        if rows or root.name not in self.result["container_states"]:
            self.result["container_states"][root.name] = sorted(
                [{key: row[key] for key in ("service", "state", "running", "health")} for row in rows],
                key=lambda row: str(row["service"]))
        return rows

    def assert_no_bot_started(self, rows):
        bots = [row for row in rows if row["service"] == "bot"]
        if len(bots) != 1 or bots[0]["running"] or not bots[0]["started_at"].startswith("0001-"):
            raise AcceptanceError("empty_credentials_bot_was_started")

    def assert_restored_writers_held(self, rows):
        by_service = {row["service"]: row for row in rows}
        for service in RESTORE_TASK_WRITERS:
            row = by_service.get(service, {})
            if not row or row.get("running") or not row.get("started_at", "").startswith("0001-"):
                raise AcceptanceError("restored_task_writer_started_before_cutover")

    def approve_restored_cutover(self):
        config = self.root / "restored-private-config.json"
        value = read_json(config)
        # The synthetic token remains empty: approval releases queue services,
        # while the Bot itself must never execute in this acceptance run.
        value["env"]["START_BOT"] = "true"
        atomic_json(config, value)
        self.command([sys.executable, str(self.source / "install.py"), "resume", "--root", str(self.restored),
                      "--config", str(config)], "restored-cutover-resume", timeout=1800, progress_root=self.restored)

    def install(self, root, restore=None):
        values = allocate_ports()
        values.update({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_ALLOWED_CHAT_IDS": "", "SYNOLOGY_CHAT_ENABLED": "false",
                       "SYNOLOGY_CHAT_OUTGOING_TOKEN": "", "SYNOLOGY_CHAT_INCOMING_WEBHOOK_URL": "",
                       "START_BOT": "false", "WRAPPER_SINGLE_ENABLE": "false", "WRAPPER_DEVICE_INFO": "",
                       "VM_MEMORY": "1536", "VM_CPUS": "1" if restore else "2", "VM_DISK_SIZE": "8G", "WORD_LYRICS_NAS_THREADS": "2",
                       "INSTALL_MIN_FREE_GIB": str(self.min_free_gib), "INSTALL_MIN_AVAILABLE_GIB": "4"})
        config = self.root / (root.name + "-private-config.json")
        atomic_json(config, {"env": values})
        command = [sys.executable, str(self.source / "install.py"), "install", "--root", str(root),
                   "--with-media-servers", "--config", str(config)]
        if restore:
            command += ["--restore", str(restore)]
        self.command(command, root.name + "-install", timeout=14400, progress_root=root)

    def reconfigure_original_vm(self):
        config = self.root / "original-private-config.json"
        value = read_json(config)
        value["env"]["VM_CPUS"] = "1"
        atomic_json(config, value)
        self.command([sys.executable, str(self.source / "install.py"), "resume", "--root", str(self.original),
                      "--config", str(config)], "original-config-resume", timeout=1800, progress_root=self.original)

    def http(self, port, path, accepted=(200,), payload=None, headers=None):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request("http://127.0.0.1:" + str(port) + path, data=body,
                                         headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with opener.open(request, timeout=5) as response:
                status, content = response.status, response.read(1024 * 1024)
        except urllib.error.HTTPError as error:
            status, content = error.code, error.read(1024 * 1024)
            error.close()
        if status not in accepted:
            raise AcceptanceError("unexpected_local_http_status_" + str(status))
        return content

    def service_checks(self, root, rows, writers_held=False):
        self.readiness_step = "core_container_states"
        values = read_env(root / "config/private.env")
        by_service = {row["service"]: row for row in rows}
        self.assert_no_bot_started(rows)
        expected = {"legacy-wrapper", "wrapper", "youtube-pot", "lxserver", "sq-mysql", "sq-neteasecloudmusicapi",
                    "sqmusic-server", "word-lyrics-resolver", "word-lyrics-nas-aligner", "navidrome", "emby", "lrcapi"}
        if writers_held:
            self.assert_restored_writers_held(rows)
            expected.difference_update(RESTORE_TASK_WRITERS)
        if any(not by_service.get(service, {}).get("running") for service in expected):
            raise AcceptanceError("core_service_not_running")
        healthy = ("youtube-pot", "sq-mysql") if writers_held else ("youtube-pot", "lxserver", "sq-mysql")
        if any(by_service[service]["health"] != "healthy" for service in healthy):
            raise AcceptanceError("core_healthcheck_not_ready")
        self.readiness_step = "po_ping"
        self.command(["docker", "exec", by_service["youtube-pot"]["id"], "node", "-e",
                      "fetch('http://127.0.0.1:4416/ping').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"],
                     root.name + "-po-ping", timeout=15)
        self.readiness_step = "sq_database_initialization"
        sql = "SELECT CONCAT(config_key,'=',config_value) FROM sq_config WHERE config_key IN ('system.download.num','system.download.timeout.retry.num');\n"
        query = self.command(["docker", "exec", "-i", by_service["sq-mysql"]["id"], "sh", "-c",
                              'export MYSQL_PWD="$MYSQL_PASSWORD"; exec mysql --batch --skip-column-names -usqmusic sqmusicv3'],
                             root.name + "-sq-initialization", capture=True, input_text=sql, timeout=20)
        settings = set(query.stdout.strip().splitlines())
        if not {"system.download.num=1", "system.download.timeout.retry.num=0"}.issubset(settings):
            raise AcceptanceError("sq_initialization_not_applied")
        if not writers_held:
            auth = read_json(Path(values["SQ_AUTH_PATH"]))
            self.readiness_step = "sq_private_login"
            login = json.loads(self.http(values["SQ_HOST_PORT"], "/api/config/login", payload={
                "username": auth["username"], "password": auth["password"], "device": "acceptance-harness"}))
            data = login.get("data") or {}
            if login.get("code") != 200 or not (data.get("tokenValue") or data.get("token")):
                raise AcceptanceError("sq_private_login_failed")
            lx_auth = Path(values["LX_FRONTEND_AUTH_PATH"]).read_text(encoding="utf-8").strip()
            self.readiness_step = "lx_authenticated_api"
            self.http(values["LX_HOST_PORT"], "/api/config", headers={"x-frontend-auth": lx_auth, "x-user-name": "default"})
        self.readiness_step = "navidrome_http"
        self.http(values["NAVIDROME_HOST_PORT"], "/ping")
        self.readiness_step = "emby_http"
        self.http(values["EMBY_HOST_PORT"], "/emby/System/Info/Public", accepted=(200, 401))
        self.readiness_step = "lrcapi_static_http"
        self.http(values["LRCAPI_HOST_PORT"], "/src")
        self.readiness_step = "mount_relocation"
        self.assert_mounts(root, rows)
        return True

    def assert_mounts(self, root, rows):
        for row in rows:
            mounts = json.loads(self.command(["docker", "inspect", "--format", "{{json .Mounts}}", row["id"]],
                                            root.name + "-mounts", capture=True).stdout)
            for mount in mounts:
                source = Path(mount.get("Source", "/"))
                if str(source) == "/var/run/docker.sock":
                    continue
                try:
                    source.relative_to(root)
                except ValueError:
                    raise AcceptanceError("service_mount_leaves_new_installation_root") from None
                if root == self.restored and (source == self.original or self.original in source.parents):
                    raise AcceptanceError("restored_mount_still_points_to_old_root")

    def guest_check(self, root, rows, marker=None, expected_cpus=None):
        values = read_env(root / "config/private.env")
        vm = next(row for row in rows if row["service"] == "legacy-wrapper")
        bindings = json.loads(self.command(["docker", "inspect", "--format", "{{json .NetworkSettings.Ports}}", vm["id"]],
                                          root.name + "-vm-port", capture=True).stdout)
        if not any(item.get("HostIp") == "127.0.0.1" and item.get("HostPort") == values["VM_SSH_HOST_PORT"] for item in (bindings.get("2222/tcp") or [])):
            raise AcceptanceError("guest_ssh_port_not_owned_by_test_vm")
        key = Path(values["VM_DATA_PATH"]) / "id_ed25519"
        if not key.is_file():
            raise AcceptanceError("guest_private_key_not_yet_created")
        command = ["ssh", "-i", str(key), "-p", values["VM_SSH_HOST_PORT"], "-o", "IdentitiesOnly=yes",
                   "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5",
                   "-o", "LogLevel=ERROR",
                   "-o", "UserKnownHostsFile=" + str(root / "state/acceptance-known-hosts"), "vmuser@127.0.0.1"]
        guest = "test \"$(uname -s)\" = Linux && systemctl is-active --quiet apple-wrapper-manager.service"
        if expected_cpus is not None:
            if not isinstance(expected_cpus, int) or not 1 <= expected_cpus <= 64:
                raise AcceptanceError("invalid_expected_guest_cpu_count")
            guest += " && test \"$(nproc)\" -eq " + str(expected_cpus)
        if marker is not None:
            # Marker is generated here as hex, never interpolated user input.
            if not all(character in "0123456789abcdef" for character in marker):
                raise AcceptanceError("invalid_synthetic_marker")
            guest += " && printf '%s' '" + marker + "' | sudo tee /var/lib/amdl-acceptance-marker >/dev/null"
        else:
            guest += " && sudo cat /var/lib/amdl-acceptance-marker"
        result = self.command(command + [guest], root.name + "-real-guest", capture=True, timeout=20)
        self.readiness_step = "guest_manager_http2"
        with socket.create_connection(("127.0.0.1", int(values["VM_MANAGER_PORT"])), timeout=5) as connection:
            connection.sendall(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n" + b"\x00\x00\x00\x04\x00\x00\x00\x00\x00")
            header = b""
            while len(header) < 9:
                part = connection.recv(9 - len(header))
                if not part:
                    break
                header += part
            if len(header) != 9 or header[3] != 4 or header[5:9] != b"\x00\x00\x00\x00":
                raise AcceptanceError("guest_manager_http2_settings_not_received")
        return result.stdout.strip()

    def wait_ready(self, root, marker=None, writers_held=False, expected_cpus=None):
        deadline = time.monotonic() + self.timeout
        core_checked = False
        while time.monotonic() < deadline:
            try:
                rows = self.save_states(root)
                self.assert_no_bot_started(rows)
                if not core_checked:
                    self.service_checks(root, rows, writers_held=writers_held)
                    core_checked = True
                self.readiness_step = "real_guest_ssh_and_manager_service"
                guest_marker = self.guest_check(root, rows, marker, expected_cpus=expected_cpus)
                self.service_checks(root, rows, writers_held=writers_held)
                self.result["checks"][root.name + ("_cutover_hold_and_infrastructure" if writers_held else "_core_and_media_http")] = True
                self.result["checks"][root.name + "_real_guest_and_manager"] = True
                self.result["checks"][root.name + "_bot_never_started"] = True
                return guest_marker
            except (AcceptanceError, OSError, ValueError, urllib.error.URLError, StopIteration) as error:
                self.result["last_readiness_failure"] = {"installation": root.name,
                    "step": getattr(self, "readiness_step", "container_inventory"), "category": safe_failure(error)}
                if isinstance(error, AcceptanceError) and str(error) in FATAL_READINESS_ERRORS:
                    raise
                time.sleep(5)
        raise AcceptanceError("real_services_or_guest_readiness_timeout")

    def write_fixture(self):
        values = read_env(self.original / "config/private.env")
        marker = secrets.token_hex(24)
        state = Path(values["STATE_PATH"])
        atomic_json(state / "acceptance-marker.json", {"synthetic": True, "marker": marker})
        media = Path(values["MUSIC_PATH"]) / ".acceptance-marker"
        media.write_text(marker, encoding="utf-8")
        os.chown(str(media), int(values["PUID"]), int(values["PGID"]))
        os.chmod(str(media), 0o600)
        database_path = state / "task-cards.sqlite3"
        with sqlite3.connect(str(database_path)) as database:
            database.executescript(source_task_schema(self.source))
            cursor = database.execute("INSERT INTO task_cards (job_key,job_json,source,chat_id,kind,mode,title,created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("synthetic-acceptance-" + marker, json.dumps({"synthetic": True, "marker": marker}), "acceptance", "local-fixture", "acceptance_fixture", "none", "Synthetic fixture; no download", time.time()))
            identifier = cursor.lastrowid
            database.commit()
            database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.expected = {"marker": marker, "task_id": identifier,
                         "sq_auth": hashlib.sha256(Path(values["SQ_AUTH_PATH"]).read_bytes()).hexdigest(),
                         "lx_auth": hashlib.sha256(Path(values["LX_FRONTEND_AUTH_PATH"]).read_bytes()).hexdigest()}
        return marker

    def verify_fixture(self, runtime_media=False):
        values = read_env(self.restored / "config/private.env")
        state = Path(values["STATE_PATH"])
        if read_json(state / "acceptance-marker.json").get("marker") != self.expected["marker"]:
            raise AcceptanceError("persistent_state_marker_changed")
        if (Path(values["MUSIC_PATH"]) / ".acceptance-marker").read_text() != self.expected["marker"]:
            raise AcceptanceError("included_media_marker_changed")
        if runtime_media:
            rows = self.owned_containers(self.restored)
            aligner = next((row for row in rows if row["service"] == "word-lyrics-nas-aligner" and row["running"]), None)
            if aligner is None:
                raise AcceptanceError("restored_media_reader_not_running")
            # Run as the selected media UID, not the privileged installer. The
            # marker travels on stdin and only an exit status is returned.
            check = ("import pathlib,sys\nexpected=sys.stdin.read()\n"
                     "try:\n result=pathlib.Path('/music/.acceptance-marker').read_text()==expected\n"
                     "except (OSError,UnicodeError):\n result=False\n"
                     "sys.exit(0 if result else 1)\n")
            self.command(["docker", "exec", "--user", str(int(values["PUID"])) + ":" + str(int(values["PGID"])), "-i", aligner["id"],
                          "python", "-c", check], "restored-media-user-read", input_text=self.expected["marker"], timeout=20)
            self.result["checks"]["restored_private_media_readable_by_selected_uid"] = True
        with sqlite3.connect((state / "task-cards.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as database:
            row = database.execute("SELECT id,status,job_json FROM task_cards WHERE id=?", (self.expected["task_id"],)).fetchone()
        if not row or row[1] != "queued" or json.loads(row[2]).get("marker") != self.expected["marker"]:
            raise AcceptanceError("persistent_task_id_or_state_changed")
        for name, key in (("sq_auth", "SQ_AUTH_PATH"), ("lx_auth", "LX_FRONTEND_AUTH_PATH")):
            if hashlib.sha256(Path(values[key]).read_bytes()).hexdigest() != self.expected[name]:
                raise AcceptanceError("restored_private_credentials_changed")
        self.result["checks"].update({"state_marker_preserved": True, "synthetic_task_id_and_queued_state_preserved": True,
                                       "included_media_preserved": True, "lx_sq_credentials_preserved": True,
                                       "restored_mounts_relocated": True})

    def cleanup_containers(self, root):
        rows = self.owned_containers(root)
        ids = [row["id"] for row in rows]
        if ids:
            self.command(["docker", "stop", "--time", "90", *ids], root.name + "-stop-owned", timeout=180, allow_failure=True)
            self.command(["docker", "rm", "--force", *ids], root.name + "-remove-owned", timeout=180)
        # Do not prune global images, volumes or networks. Data, bundle and
        # raw logs remain private for inspection on this disposable host.

    def safe_diagnostics(self):
        private_values = []
        for root in (self.original, self.restored):
            try:
                values = read_env(root / "config/private.env")
                private_values.extend(credential_values(values))
                for key in ("LX_FRONTEND_AUTH_PATH", "SQ_AUTH_PATH"):
                    path = Path(values.get(key, "/nonexistent"))
                    if path.is_file() and not path.is_symlink():
                        text = path.read_text(encoding="utf-8")
                        if key == "SQ_AUTH_PATH":
                            private_values.extend(value for value in json.loads(text).values() if isinstance(value, str) and value)
                        else:
                            private_values.append(text.strip())
            except (OSError, ValueError):
                pass
        candidates, summaries = [], []
        for root in (self.original, self.restored):
            state = read_json(root / "state/install.json")
            failed = state.get("failed_stage")
            if failed in STAGES:
                self.result.setdefault("installer_failures", []).append({"installation": root.name,
                    "stage": failed})
                for label in STAGE_DIAGNOSTIC_LOGS.get(failed, ()):
                    candidates.append((root.name + ":" + failed + ":" + label, root / "state/logs" / (label + ".log")))
            for label in (root.name + "-install", "restored-cutover-resume" if root == self.restored else "original-config-resume"):
                if label:
                    summaries.append((root.name + ":installer", self.root / "private-logs" / (label + ".log")))
        safe = []
        for label, path in candidates + summaries:
            if not path.is_file() or path.is_symlink():
                continue
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 65536))
                lines = handle.read(65536).decode("utf-8", errors="replace").splitlines()
            for line in lines:
                # Only the failed installer stage and installer summaries,
                # never successful build logs, container
                # application logs, Cookie files, Config.Env, SQL or API bodies.
                if re.search(r"(?i)cookie|authorization|bearer|password|secret|token|private.key", line):
                    continue
                if not re.search(r"(?i)installation stage|installation incomplete|error|failed|fatal|could not|not found|no matching distribution|did not complete|exception|traceback|^\s*E:", line):
                    continue
                safe.append("[" + label + "] " + redact_diagnostic(line, private_values, self.root, self.source))
                if len(safe) >= 60:
                    break
            if len(safe) >= 60:
                break
        self.result["safe_build_diagnostics"] = safe

    def run(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        started = time.time()
        try:
            with isolated_environment(self.source):
                self.progress("fresh_install")
                self.install(self.original)
                self.progress("fresh_real_readiness")
                # Create a marker before the guest check writes its copy.
                marker = self.write_fixture()
                self.wait_ready(self.original, marker=marker)
                self.progress("configuration_resume")
                self.reconfigure_original_vm()
                if self.wait_ready(self.original, expected_cpus=1) != marker:
                    raise AcceptanceError("vm_guest_marker_changed_after_reconfigure")
                self.result["checks"].update({"configuration_resume_applied": True,
                                              "vm_disk_preserved_after_reconfigure": True})
                self.progress("offline_snapshot")
                manifest = manage.backup(self.original, self.bundle, include_media=True,
                                         include_images=self.include_images, restart=False)
                if manifest.get("complete") is not True or manifest.get("include_media") is not True:
                    raise AcceptanceError("snapshot_coverage_incomplete")
                self.result["checks"]["real_offline_snapshot_validated"] = True
                self.progress("remove_original_owned_containers")
                self.cleanup_containers(self.original)
                if self.owned_containers(self.original):
                    raise AcceptanceError("original_owned_containers_still_present")
                self.result["checks"]["original_owned_containers_removed_before_restore"] = True
                self.progress("restore_install")
                self.install(self.restored, restore=self.bundle)
                self.progress("restored_cutover_hold")
                guest_marker = self.wait_ready(self.restored, writers_held=True)
                if guest_marker != marker:
                    raise AcceptanceError("restored_vm_guest_marker_changed")
                self.result["checks"]["vm_guest_disk_preserved"] = True
                self.verify_fixture()
                self.result["checks"]["restored_queue_held_before_approval"] = True
                self.progress("explicit_cutover_approval")
                self.approve_restored_cutover()
                self.progress("restored_real_readiness")
                self.wait_ready(self.restored)
                self.verify_fixture(runtime_media=True)
                self.result["checks"]["restored_queue_services_released_after_approval"] = True
                self.result["passed"] = True
                self.result["stage"] = "completed"
        except Exception as error:
            self.result["stage"] = self.phase
            self.result["failure"] = safe_failure(error)
        finally:
            self.result["duration_seconds"] = round(time.time() - started)
            for root in (self.original, self.restored):
                try:
                    if (root / "deployment.json").is_file():
                        self.save_states(root)
                        if not self.keep_containers:
                            self.cleanup_containers(root)
                except Exception:
                    self.result.setdefault("cleanup_attention", []).append(root.name)
            self.result["private_artifacts_uploaded"] = False
            self.result["containers_kept"] = self.keep_containers
            if not self.result["passed"]:
                try:
                    self.safe_diagnostics()
                except Exception:
                    self.result["diagnostics_attention"] = "safe_diagnostic_extraction_failed"
            atomic_json(self.report_path, self.result)
        return self.result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="new or empty isolated acceptance workspace")
    parser.add_argument("--report", type=Path, required=True, help="sanitized JSON; the only CI artifact to upload")
    parser.add_argument("--include-images", action="store_true", help="exercise full image-archive backup/restore too")
    parser.add_argument("--timeout", type=int, default=1200, help="readiness timeout per installation in seconds")
    parser.add_argument("--min-free-gib", type=int, default=20, help="explicit disposable-runner disk threshold")
    parser.add_argument("--keep-containers", action="store_true", help="leave only this test's labeled containers for private debugging")
    args = parser.parse_args(argv)
    if args.timeout < 1 or args.min_free_gib < 8:
        parser.error("timeout must be positive and disk threshold at least 8 GiB")
    os.umask(0o077)
    try:
        result = Harness(args.root, args.report, args.include_images, args.timeout,
                         args.keep_containers, args.min_free_gib).run()
    except (AcceptanceError, OSError, ValueError):
        print("Acceptance refused: use a new private workspace and a safe report path.", file=sys.stderr)
        return 1
    print("Acceptance " + ("passed" if result["passed"] else "failed") + "; inspect the sanitized report.")
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
