"""Install an isolated stack without placing credentials in its source tree."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

SOURCE = Path(__file__).resolve().parents[1]
SCHEMA = 1
OWNER_LABEL = "io.apple-music-downloader.installation"
PROFILES = ["auth", "lyrics", "lx", "sq", "self-heal"]
SERVICES = ["legacy-wrapper", "wrapper", "youtube-pot", "bot", "word-lyrics-resolver",
            "word-lyrics-nas-aligner", "youtube-auth-browser", "bilibili-auth-browser",
            "lxserver", "sq-mysql", "sq-neteasecloudmusicapi", "sqmusic-server",
            "wrapper-watchdog", "bot-watchdog"]
RESTORE_TASK_WRITERS = ("bot", "word-lyrics-resolver", "word-lyrics-nas-aligner", "lxserver", "sqmusic-server",
                        "wrapper-watchdog", "bot-watchdog")
RESTORE_QUEUE_SERVICES = ("lxserver", "sqmusic-server", "word-lyrics-resolver", "word-lyrics-nas-aligner")
STAGES = ["restore", "directories", "source", "configuration", "credentials", "images", "dependencies",
          "build", "pull", "containers", "services", "sq_configuration", "activation", "verification"]
SECRET_KEYS = {"TELEGRAM_BOT_TOKEN", "SYNOLOGY_CHAT_OUTGOING_TOKEN", "SYNOLOGY_CHAT_INCOMING_WEBHOOK_URL",
               "SQ_DB_PASSWORD", "SQ_DB_ROOT_PASSWORD", "SQ_JWT_SECRET", "NAVIDROME_PASSWORD", "EMBY_PASSWORD"}
PORTS = {"BOT_HEALTH_PORT": 30871, "VM_MANAGER_PORT": 18080, "VM_SSH_HOST_PORT": 22221,
         "YOUTUBE_BROWSER_PORT": 31011, "BILIBILI_BROWSER_PORT": 31012,
         "LX_HOST_PORT": 19527, "SQ_HOST_PORT": 18099}


def validate_root(root):
    root = Path(root).expanduser()
    if not root.is_absolute() or ".." in root.parts or root == Path(root.anchor):
        raise ValueError("--root must be an absolute dedicated directory")
    if len(root.parts) < 3 or root.name in {"docker", "LocalWork", "Documents", "Desktop"}:
        raise ValueError("use a dedicated installation directory below shared storage")
    if (root / ".git").exists():
        raise ValueError("use a data directory outside the development checkout")
    for path in [root] + list(root.parents):
        if path.is_symlink():
            raise ValueError("installation root and its parents must not be symlinks")
    if any(part in {"Documents", "Desktop", "iCloud Drive", "CloudStorage"} for part in root.parts):
        raise ValueError("use a local deployment disk, outside cloud-synced document folders")
    return root


def read_json(path, default=None):
    if not path.exists():
        return {} if default is None else default
    if path.is_symlink():
        raise ValueError("managed JSON files must not be symlinks")
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name, dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_env(path):
    values = {}
    if path.is_symlink():
        raise ValueError("private environment file must not be a symlink")
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quote = value[0]
            value = value[1:-1]
            if quote == "'":
                value = value.replace("\\'", "'")
        values[key.strip()] = value
    return values


def write_env(path, values):
    # Compose dotenv single quotes prevent dollar interpolation in credentials.
    lines = []
    for key, value in sorted(values.items()):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or any(c in str(value) for c in "\r\n\x00"):
            raise ValueError("environment values must be single-line strings with uppercase keys")
        lines.append(key + "='" + str(value).replace("'", "\\'") + "'\n")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".private.env.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_config(path):
    if path is None:
        return {}
    value = read_json(Path(path))
    value = value.get("env", value)
    if not isinstance(value, dict):
        raise ValueError("private config must contain an env object")
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("invalid private config environment key")
        if not isinstance(item, (str, int, bool)):
            raise ValueError("private config values must be strings, integers or booleans")
        text = str(item).lower() if isinstance(item, bool) else str(item)
        if any(c in text for c in "\r\n\x00"):
            raise ValueError("private config values must be single-line")
        result[key] = text
    return result


def import_helper(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def available_memory_gib():
    fields = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            fields[key] = int(value.strip().split()[0])
        return fields.get("MemAvailable", fields["MemFree"]) / (1024 ** 2)
    except (OSError, ValueError, KeyError):
        raise RuntimeError("cannot determine Linux available RAM") from None


@contextmanager
def install_lock(root):
    directory = root / "state"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "install.lock"
    if path.is_symlink():
        raise ValueError("installation lock must not be a symlink")
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another installer is running for this root") from None
        yield
    finally:
        os.close(descriptor)


class Installer:
    def __init__(self, root, config=None, restore=None, with_media_servers=False,
                 dry_run=False, runner=subprocess.run, source_dir=SOURCE):
        self.root = validate_root(root)
        for name in ("source", "state", "config", "private", "data", "media", "cache"):
            if (self.root / name).is_symlink():
                raise ValueError("managed directories must not be symlinks")
        self.source = self.root / "source"
        self.origin = Path(source_dir)
        if self.root == self.origin.resolve() or self.origin.resolve() in self.root.parents:
            raise ValueError("installation root must be outside the source checkout")
        self.env_file = self.root / "config/private.env"
        self.state_file = self.root / "state/install.json"
        self.descriptor_file = self.root / "deployment.json"
        self.config_path = Path(config) if config else None
        self.config = load_config(config)
        self.restore = Path(restore) if restore else None
        self.dry_run = dry_run
        self.runner = runner
        self.state = read_json(self.state_file)
        self.descriptor = read_json(self.descriptor_file)
        self.project = self.descriptor.get("project_name") or self.config.get("COMPOSE_PROJECT_NAME") or "amdl-" + hashlib.sha256(str(self.root).encode()).hexdigest()[:10]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", self.project):
            raise ValueError("COMPOSE_PROJECT_NAME must be a safe lowercase project name")
        self.owner = self.descriptor.get("installation_id") or self.state.get("installation_id") or secrets.token_hex(16)
        self.media = bool(with_media_servers or self.state.get("with_media_servers"))
        self._logging = False

    def journal(self, stage, event):
        path = self.root / "state/install.journal.jsonl"
        descriptor = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": time.time(), "stage": stage, "event": event}) + "\n")

    def run(self, arguments, label, timeout=1800, allow_failure=False, capture=True):
        from deploy.environment import compose_environment
        template = (self.source if self.source.exists() else self.origin) / ".env.example"
        log = self.root / "state/logs" / (re.sub(r"[^a-z0-9_-]", "_", label.lower()) + ".log")
        private_output = None
        if not capture and self._logging:
            log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            private_output = os.fdopen(descriptor, "w", encoding="utf-8")
        try:
            result = self.runner(list(arguments), cwd=str(self.source if self.source.exists() else self.origin),
                                 stdin=subprocess.DEVNULL, stdout=private_output or subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, check=False, timeout=timeout,
                                 env=compose_environment(self.env_file, template, extra_keys=self.config))
        finally:
            if private_output:
                private_output.close()
        if capture and self._logging and self.root.exists():
            log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(result.stdout or "")
        if result.returncode and not allow_failure:
            raise RuntimeError(label + " failed; inspect its private installation log")
        return result

    def compose(self, *arguments, **options):
        files = self.descriptor.get("compose_files", ["compose.yaml", "services/lx-music/compose.yaml",
                  "services/sq-music/compose.yaml", "compose.self-heal.yaml"])
        command = ["docker", "compose", "--project-directory", str(self.source),
                   "--env-file", str(self.env_file), "--project-name", self.project]
        for filename in files:
            path = Path(filename)
            command += ["-f", str(path if path.is_absolute() else self.source / path)]
        extras = list(self.descriptor.get("extra_compose_files", []))
        if (self.root / "config/images.compose.json").is_file() and "config/images.compose.json" not in extras:
            extras.append("config/images.compose.json")
        for filename in extras:
            command += ["-f", str(self.root / filename)]
        for profile in self.descriptor.get("profiles", PROFILES):
            command += ["--profile", profile]
        return self.run(command + list(arguments), options.pop("label", "compose"), **options)

    def preflight(self):
        if sys.version_info < (3, 8):
            raise RuntimeError("host Python 3.8 or newer is required")
        if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
            raise RuntimeError("this stack requires a Linux x86_64 host")
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            raise RuntimeError("/dev/kvm must exist and be readable/writable by the installer")
        existing = self.root
        while not existing.exists():
            existing = existing.parent
        if not os.access(str(existing), os.W_OK):
            raise RuntimeError("installation parent is not writable")
        settings = dict(read_env(self.env_file), **self.config)
        if shutil.disk_usage(str(existing)).free < int(settings.get("INSTALL_MIN_FREE_GIB", "50")) * 1024 ** 3:
            raise RuntimeError("insufficient free disk space; default requirement is 50 GiB")
        memory = available_memory_gib()
        if memory < float(settings.get("INSTALL_MIN_AVAILABLE_GIB", "6")):
            raise RuntimeError("insufficient available RAM; default requirement is 6 GiB")
        if not shutil.which("git"):
            raise RuntimeError("git is required to fetch the fixed SQ source revision")
        for path in (self.root, self.root / "config", self.root / "data", self.root / "private"):
            if path.is_symlink():
                raise RuntimeError("managed directories must not be symlinks")
            if path.exists() and path.stat().st_uid != os.geteuid() and os.geteuid() != 0:
                raise RuntimeError("managed directories must belong to the installing user")
        version = self.run(["docker", "compose", "version", "--short"], "compose-version", timeout=30).stdout.strip()
        match = re.search(r"(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (2, 20):
            raise RuntimeError("Docker Compose 2.20 or newer is required")
        self.run(["docker", "info", "--format", "{{.ServerVersion}}"], "docker-daemon", timeout=30)
        self.run(["git", "--version"], "git-prerequisite", timeout=30)
        self.check_existing_project()
        values = dict(read_env(self.env_file), **self.config)
        ports = dict(PORTS)
        if self.media:
            ports.update({"NAVIDROME_HOST_PORT": 14533, "EMBY_HOST_PORT": 18096, "LRCAPI_HOST_PORT": 28883})
        if len({int(values.get(key, default)) for key, default in ports.items()}) != len(ports):
            raise RuntimeError("configured loopback ports must be distinct")
        # Existing installations own their published ports; Docker validates
        # their bindings again on recreation. New roots may not steal ports.
        if not self.descriptor:
            for key, default in ports.items():
                try:
                    with socket.socket() as listener:
                        listener.bind(("127.0.0.1", int(values.get(key, default))))
                except OSError:
                    raise RuntimeError(key + " is occupied; choose a different private config port") from None
        return {"platform": "linux/amd64", "kvm": True, "compose": version, "available_memory_gib": round(memory, 2)}

    def check_existing_project(self):
        result = self.run(["docker", "ps", "-a", "--filter", "label=com.docker.compose.project=" + self.project,
                           "--format", "{{.ID}}"], "project-ownership", timeout=30)
        ids = result.stdout.split()
        if ids:
            if not self.descriptor:
                raise RuntimeError("this project name already belongs to an unmanaged installation")
            for identifier in ids:
                labels = self.run(["docker", "inspect", "--format", "{{json .Config.Labels}}", identifier],
                                  "container-ownership", timeout=30).stdout
                if json.loads(labels or "{}").get(OWNER_LABEL) != self.owner:
                    raise RuntimeError("refusing to manage a container owned by another installation")

    def install(self):
        if self.dry_run:
            return {"dry_run": True, "root": str(self.root), "stages": STAGES,
                    "with_media_servers": self.media, "containers_changed": False}
        with install_lock(self.root):
            self._logging = True
            self.preflight()
            self.state["completed"] = [stage for stage in self.state.get("completed", []) if stage not in {"configuration", "activation", "verification"}]
            self.state.update({"schema": SCHEMA, "installation_id": self.owner,
                               "with_media_servers": self.media, "status": "installing"})
            atomic_json(self.state_file, self.state)
            for stage in STAGES:
                if stage in self.state.get("completed", []):
                    continue
                print("Installation stage: " + stage, flush=True)
                self.journal(stage, "started")
                try:
                    getattr(self, "stage_" + stage)()
                except BaseException:
                    self.state.update({"status": "incomplete", "failed_stage": stage})
                    atomic_json(self.state_file, self.state)
                    self.journal(stage, "failed")
                    raise
                self.state.setdefault("completed", []).append(stage)
                self.state.pop("failed_stage", None)
                atomic_json(self.state_file, self.state)
                self.journal(stage, "completed")
                print("Installation stage completed: " + stage, flush=True)
            self.state["status"] = "installed"
            atomic_json(self.state_file, self.state)
            return self.status()

    def stage_restore(self):
        if self.restore:
            from deploy.migration import restore_bundle
            self.state["restore"] = restore_bundle(self.restore, self.root, plan_validator=self.normalize_restore)
            self.state["restored_needs_cutover"] = True

    def normalize_restore(self, plan):
        from deploy.restore_plan import load_service_mapping, normalize_restore_plan
        mapping = load_service_mapping(self.config_path)
        # An omitted mapping on resume preserves the reviewed saved mapping.
        # Original source records remain available to revalidate all records.
        if not mapping:
            mapping = plan.get("service_mapping", {})
        allowed = list(SERVICES) + (["navidrome", "emby", "lrcapi"] if self.media else [])
        return normalize_restore_plan(plan, mapping, allowed_services=allowed)

    def stage_directories(self):
        for name in ("config", "private", "data", "media", "models", "cache", "state/logs"):
            path = self.root / name
            if path.is_symlink():
                raise ValueError("managed directories must not be symlinks")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def stage_source(self):
        if self.source.is_symlink():
            raise RuntimeError("source checkout must not be a symlink")
        if self.origin.resolve() == self.source.resolve():
            return
        if self.source.exists():
            if not (self.source / "compose.yaml").is_file():
                raise RuntimeError("source directory already contains unrelated files")
            return
        # Release directories contain no runtime state. Also filter a local
        # checkout defensively, including nested dependency/build directories.
        allowed = {"bot", "deploy", "ops", "services", "vm", "scripts", "tests", "docs", "licenses", ".github"}
        base_ignore = shutil.ignore_patterns(".git", ".venv*", "__pycache__", "*.pyc", "node_modules",
                                             "runtime", "secrets", "upstream", "patched", "outputs", "build", "dist")
        def ignore(directory, names):
            excluded = set(base_ignore(directory, names))
            excluded.update(name for name in names if (Path(directory) / name).is_symlink()
                            or (name.startswith(".env") and name != ".env.example"))
            excluded.update(name for name in names if name.startswith("private-config") and not name.endswith(".example.json"))
            excluded.update(name for name in names if name == "private.json")
            return excluded
        tracked = None
        if (self.origin / ".git").exists():
            tracked = self.run(["git", "-C", str(self.origin), "ls-files", "-z"], "source-tracked-files", timeout=30).stdout.split("\0")
        with tempfile.TemporaryDirectory(prefix=".source-prepare-", dir=str(self.root)) as temporary:
            prepared = Path(temporary) / "source"
            prepared.mkdir(mode=0o700)
            if tracked is not None:
                for relative in tracked:
                    if not relative:
                        continue
                    parts = Path(relative).parts
                    if Path(relative).is_absolute() or ".." in parts:
                        raise RuntimeError("tracked source path is unsafe")
                    if any(part in ignore(str(self.origin), list(parts)) for part in parts):
                        continue
                    item = self.origin / relative
                    if any(path.is_symlink() for path in [item] + list(item.parents) if path != self.origin and self.origin in path.parents):
                        continue
                    if item.is_file():
                        target = prepared / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(item), str(target))
                os.replace(str(prepared), str(self.source))
                return
            for item in self.origin.iterdir():
                if item.is_symlink():
                    continue
                if item.is_dir() and item.name in allowed:
                    shutil.copytree(str(item), str(prepared / item.name), ignore=ignore)
                elif item.is_file() and (item.name.startswith(("README", "compose", "requirements", "LICENSE"))
                                         or item.name in {"Dockerfile", ".dockerignore", ".gitignore", ".env.example", "install.sh", "install.py",
                                                          "THIRD_PARTY_NOTICES.md", "SECURITY.md", ".gitattributes"}):
                    shutil.copy2(str(item), str(prepared / item.name))
            os.replace(str(prepared), str(self.source))

    def managed_paths(self):
        mapping = {"STATE_PATH": "data/state", "MUSIC_PATH": "media/music", "YOUTUBE_MUSIC_PATH": "media/youtube-music",
                   "YOUTUBE_VIDEO_PATH": "media/youtube-video", "BILIBILI_MUSIC_PATH": "media/bilibili-music",
                   "BILIBILI_VIDEO_PATH": "media/bilibili-video", "VM_DATA_PATH": "data/vm",
                   "WRAPPER_SESSION_PATH": "private/wrapper-session", "YOUTUBE_BROWSER_PATH": "private/youtube-browser",
                   "BILIBILI_BROWSER_PATH": "private/bilibili-browser", "LX_DATA_PATH": "data/lx-music",
                   "LX_FRONTEND_AUTH_PATH": "private/lx-frontend-auth", "SQ_STAGING_PATH": "data/sq-staging",
                   "SQ_MYSQL_DATA_PATH": "data/sq-mysql", "SQ_AUTH_PATH": "private/sq/sq-auth.json",
                   "NAVIDROME_DATA_PATH": "data/navidrome", "EMBY_DATA_PATH": "data/emby", "LRCAPI_DATA_PATH": "data/lrcapi"}
        return {key: str(self.root / value) for key, value in mapping.items()}

    def stage_configuration(self):
        from deploy import dependencies
        if self.state.get("restore"):
            self.state["restore"] = self.normalize_restore(self.state["restore"])
        old = read_env(self.env_file)
        values = read_env(self.source / ".env.example")
        values.update(getattr(dependencies, "DEFAULT_IMAGES", {}))
        values.update(getattr(dependencies, "DEFAULT_ENV", {}))
        values.update({key: "true" for key in ("AMDL_UNIFIED_MUSIC_ENABLED", "AMDL_LX_FALLBACK_ENABLED", "AMDL_SQ_MUSIC_ENABLED",
                      "AMDL_WORD_LYRICS_AUTOMATION_ENABLED", "AMDL_WORD_LYRICS_COMPLETION_ENABLED", "ONLINE_AUTH_AUTO_REFRESH_ENABLED",
                      "INSTALL_YT_SABR", "YT_SABR_FALLBACK_ENABLED")})
        values["START_BOT"] = "true"
        restore = self.state.get("restore", {})
        restored_paths = {}
        restored_database_credentials = {}
        for path in restore.get("env_files", {}).values():
            imported = read_env(Path(path))
            for key, value in imported.items():
                if key in {"SQ_DB_PASSWORD", "SQ_DB_ROOT_PASSWORD", "SQ_JWT_SECRET"} and value:
                    if key in restored_database_credentials and restored_database_credentials[key] != value:
                        raise ValueError("restored SQ projects contain conflicting database credentials")
                    restored_database_credentials[key] = value
                values[key] = restore.get("source_map", {}).get(value, value)
                if value in restore.get("source_map", {}):
                    restored_paths[key] = values[key]
        values.update(self.managed_paths())
        values.update(restored_paths)
        uid = os.environ.get("SUDO_UID", str(os.getuid())) if os.geteuid() == 0 else str(os.getuid())
        gid = os.environ.get("SUDO_GID", str(os.getgid())) if os.geteuid() == 0 else str(os.getgid())
        values.update({"COMPOSE_PROJECT_NAME": self.project, "PRIVATE_ENV_FILE": str(self.env_file),
                       "BOT_IMAGE": self.project + "-bot:installed", "VM_IMAGE": self.project + "-vm:installed",
                       "SQ_PATCHED_IMAGE": self.project + "-sq:3.1.33-bot", "PUID": uid, "PGID": gid,
                       "YT_OUTPUT_UID": uid, "YT_OUTPUT_GID": gid})
        for key, value in self.config.items():
            if key in self.managed_paths() and value != self.managed_paths()[key]:
                raise ValueError("managed data paths are derived from --root; restore uses explicit mount mappings")
            if key in SECRET_KEYS and old.get(key) and old[key] != value:
                raise ValueError("refusing to replace an existing private credential")
            if key in restored_database_credentials and restored_database_credentials[key] != value:
                raise ValueError("refusing to replace a restored database credential")
        values.update(old)
        values.update(self.config)
        values["PRIVATE_ENV_FILE"] = str(self.env_file)
        values["COMPOSE_PROJECT_NAME"] = self.project
        if self.state.get("restored_needs_cutover"):
            if self.config.get("START_BOT", "").lower() == "true":
                self.state["restored_needs_cutover"] = False
                self.state["cutover_approved_at"] = time.time()
            else:
                values["START_BOT"] = "false"
        for role, key in (("primary", "WORD_LYRICS_NAS_WHISPER_MODEL"), ("fallback", "WORD_LYRICS_NAS_FALLBACK_MODEL")):
            expected = "/state/word-lyrics/models/" + dependencies.MODELS[role].filename
            if values.get(key) and values[key] != expected:
                raise ValueError(key + " must use the full-suite verified model path")
            values[key] = expected
        expected_sha = dependencies.MODELS["primary"].sha1
        if values.get("WORD_LYRICS_NAS_WHISPER_MODEL_SHA1") not in (None, "", expected_sha):
            raise ValueError("the full-suite primary model checksum is fixed")
        values["WORD_LYRICS_NAS_WHISPER_MODEL_SHA1"] = expected_sha
        for key in self.managed_paths():
            path = Path(values[key])
            try:
                path.relative_to(self.root)
            except ValueError:
                raise ValueError(key + " must remain below this installation root") from None
            for component in [path] + list(path.parents):
                if component == self.root:
                    break
                if component.is_symlink():
                    raise ValueError(key + " must not cross a symlink")
        if self.media:
            for key, value in {"NAVIDROME_URL": "http://navidrome:4533", "EMBY_URL": "http://emby:8096", "YT_LRCAPI_URL": "http://lrcapi:28883"}.items():
                if not old.get(key) and key not in self.config:
                    values[key] = value
        for key in ("SQ_DB_PASSWORD", "SQ_DB_ROOT_PASSWORD", "SQ_JWT_SECRET"):
            if not values.get(key) or values[key].startswith(("GENERATE_", "REPLACE_")):
                database = Path(values["SQ_MYSQL_DATA_PATH"])
                if database.exists() and any(database.iterdir()):
                    raise RuntimeError("existing SQ database requires its original matching credentials")
                values[key] = secrets.token_hex(48 if key == "SQ_JWT_SECRET" else 24)
        for key in ("SQ_MYSQL_IMAGE", "SQ_NETEASE_IMAGE", "LX_IMAGE", "AUTH_BROWSER_IMAGE", "PO_PROVIDER_IMAGE"):
            if "@sha256:" not in values.get(key, ""):
                raise RuntimeError(key + " requires a pinned image digest")
        write_env(self.env_file, values)
        services = list(SERVICES)
        files = ["compose.yaml", "services/lx-music/compose.yaml", "services/sq-music/compose.yaml", "compose.self-heal.yaml"]
        profiles = list(PROFILES)
        if self.media:
            files.append("services/media/compose.yaml")
            profiles += ["navidrome", "emby", "lrcapi"]
            services += ["navidrome", "emby", "lrcapi"]
        override = {"services": {name: {"labels": {OWNER_LABEL: self.owner}} for name in services}}
        # Restored named volumes and bind mounts keep their container targets.
        for mount in restore.get("mounts", []):
            if mount.get("omitted") or not mount.get("restored_path"):
                continue
            name = mount["service"]
            if name not in override["services"]:
                continue
            override["services"][name].setdefault("volumes", []).append({"type": "bind", "source": mount["restored_path"],
                "target": mount["target"], "read_only": bool(mount.get("read_only")), "bind": {"create_host_path": False}})
        overlay = self.root / "config/installer.compose.json"
        for name, image in self.state.get("restored_images", {}).items():
            if name in override["services"]:
                override["services"][name]["image"] = image
        atomic_json(overlay, override)
        extra_files = ["config/installer.compose.json"]
        for name in self.descriptor.get("extra_compose_files", []):
            if name not in extra_files:
                extra_files.append(name)
        if (self.root / "config/images.compose.json").is_file() and "config/images.compose.json" not in extra_files:
            extra_files.append("config/images.compose.json")
        self.descriptor = {"schema": SCHEMA, "source_dir": "source", "env_file": "config/private.env",
                           "project_name": self.project, "installation_id": self.owner, "compose_files": files,
                           "extra_compose_files": extra_files,
                           "profiles": profiles, "owned_services": services,
                           "managed_paths": {key: values[key] for key in self.managed_paths()},
                           "media_paths": [values[key] for key in self.managed_paths() if "MUSIC_PATH" in key or "VIDEO_PATH" in key]}
        atomic_json(self.descriptor_file, self.descriptor)
        self.compose("config", "--quiet", label="compose-validation", timeout=60)

    def stage_credentials(self):
        values = read_env(self.env_file)
        uid, gid = int(values["PUID"]), int(values["PGID"])
        media_keys = {"MUSIC_PATH", "YOUTUBE_MUSIC_PATH", "YOUTUBE_VIDEO_PATH", "BILIBILI_MUSIC_PATH", "BILIBILI_VIDEO_PATH"}
        for key in self.managed_paths():
            value = values[key]
            if key not in {"LX_FRONTEND_AUTH_PATH", "SQ_AUTH_PATH"}:
                if Path(value).is_symlink():
                    raise RuntimeError("managed state must not be a symlink")
                Path(value).mkdir(parents=True, exist_ok=True, mode=0o700)
                if key in {"YOUTUBE_BROWSER_PATH", "BILIBILI_BROWSER_PATH", "NAVIDROME_DATA_PATH", "EMBY_DATA_PATH", "LRCAPI_DATA_PATH"} or "MUSIC_PATH" in key or "VIDEO_PATH" in key:
                    if os.geteuid() == 0:
                        restored_media = bool(self.state.get("restore")) and key in media_keys
                        mode = stat.S_IMODE(Path(value).stat().st_mode) if restored_media else None
                        os.chown(value, uid, gid)
                        if restored_media:
                            # Only freshly restored copies below this root are
                            # remapped. Keep restrictive modes, and change a
                            # validated internal symlink itself, not its target.
                            os.chmod(value, mode)
                            for directory, directories, files in os.walk(value, followlinks=False):
                                for name in directories + files:
                                    child = Path(directory) / name
                                    metadata = child.lstat()
                                    os.lchown(str(child), uid, gid)
                                    if not stat.S_ISLNK(metadata.st_mode):
                                        os.chmod(str(child), stat.S_IMODE(metadata.st_mode), follow_symlinks=False)
                        elif self.state.get("restore") and key in {"YOUTUBE_BROWSER_PATH", "BILIBILI_BROWSER_PATH", "NAVIDROME_DATA_PATH", "EMBY_DATA_PATH", "LRCAPI_DATA_PATH"}:
                            # These are freshly restored copies, never the
                            # old NAS originals. Chromium's 0600 SQLite files
                            # need the selected runtime UID as well as /config.
                            for directory, directories, files in os.walk(value, followlinks=False):
                                for name in directories + files:
                                    child = Path(directory) / name
                                    if child.is_symlink():
                                        raise RuntimeError("restored runtime profile requires reviewed symlink ownership")
                                    os.chown(str(child), uid, gid)
        lx = import_helper(self.source / "services/lx-music/initialize.py", "installer_lx")
        config = Path(values["LX_DATA_PATH"]) / "data/config.js"
        auth = Path(values["LX_FRONTEND_AUTH_PATH"])
        if not config.exists() and not auth.exists():
            lx.initialize(Path(values["LX_DATA_PATH"]), auth)
        elif not config.exists() or not auth.is_file():
            raise RuntimeError("existing LX data requires its matching configuration and authentication file")
        database = Path(values["SQ_MYSQL_DATA_PATH"])
        self.state["sq_new_database"] = not any(database.iterdir()) and not bool(self.state.get("restore"))
        self.run([sys.executable, str(self.source / "services/sq-music/bootstrap.py"), "prepare", "--env-file", str(self.env_file)], "sq-private-prepare")

    def stage_dependencies(self):
        from deploy import dependencies
        values = read_env(self.env_file)
        dependencies.prepare_models(Path(values["STATE_PATH"]) / "word-lyrics/models")
        if "sqmusic-server" not in self.state.get("restored_images", {}):
            result = dependencies.prepare_sq(self.source, self.root / "cache", values["SQ_PATCHED_IMAGE"], runner=self.dependency_runner)
            if result.get("image"):
                values["SQ_PATCHED_IMAGE"] = result["image"]
                write_env(self.env_file, values)

    def dependency_runner(self, arguments, **options):
        # Keep public-source/compiler diagnostics private but available to the
        # acceptance harness's strict redaction/allowlist. Git query output is
        # still captured because the dependency checker validates its values.
        if list(arguments)[:2] != ["docker", "build"]:
            return self.runner(arguments, **options)
        path = self.root / "state/logs/sq-dependency-build.log"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            options["stdout"] = output
            options["stderr"] = subprocess.STDOUT
            return self.runner(arguments, **options)

    def stage_images(self):
        restore = self.state.get("restore", {})
        archive = restore.get("image_archive")
        if not archive:
            return
        images = {}
        for record in restore.get("images", []):
            name, image = record.get("service"), record.get("image_id")
            if name not in self.descriptor.get("owned_services", []):
                continue
            if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                raise RuntimeError("restored image metadata is invalid")
            if name in images and images[name] != image:
                raise RuntimeError("different restored projects use conflicting service images; review the migration mapping")
            images[name] = image
        self.run(["docker", "image", "load", "--input", archive], "restore-image-archive", timeout=7200, capture=False)
        for image in sorted(set(images.values())):
            actual = self.run(["docker", "image", "inspect", "--format", "{{.Id}}", image], "verify-restored-image", timeout=30).stdout.strip()
            if actual != image:
                raise RuntimeError("a restored service image does not match its archived image ID")
        overlay = self.root / "config/installer.compose.json"
        value = read_json(overlay)
        for name, image in images.items():
            value["services"][name]["image"] = image
        atomic_json(overlay, value)
        self.state["restored_images"] = images

    def stage_build(self):
        restored = self.state.get("restored_images", {})
        services = [name for name in ("bot", "legacy-wrapper") if name not in restored]
        if services:
            self.compose("build", *services, label="core-build", timeout=14400, capture=False)

    def stage_pull(self):
        services = ["youtube-pot", "youtube-auth-browser", "bilibili-auth-browser", "lxserver", "sq-mysql", "sq-neteasecloudmusicapi"]
        if self.media:
            services += ["navidrome", "emby", "lrcapi"]
        services = [name for name in services if name not in self.state.get("restored_images", {})]
        if services:
            self.compose("pull", *services, label="fixed-images", timeout=7200, capture=False)

    def stage_containers(self):
        # Create every writer without starting it, including the workers held
        # during restore. Their persistent mounts must remain visible to backup.
        self.compose("create", "--no-build", "--pull", "never", "youtube-auth-browser", "bilibili-auth-browser",
                     *RESTORE_TASK_WRITERS, label="create-stopped-services")

    def stage_services(self):
        services = ["legacy-wrapper", "wrapper", "youtube-pot", "lxserver", "sq-mysql", "sq-neteasecloudmusicapi", "sqmusic-server",
                    "word-lyrics-resolver", "word-lyrics-nas-aligner"]
        if self.media:
            services += ["navidrome", "emby", "lrcapi"]
        if self.state.get("restored_needs_cutover"):
            services = [name for name in services if name not in RESTORE_TASK_WRITERS]
        self.compose("up", "-d", "--no-deps", *services, label="start-core-services", timeout=600)
        if self.state.get("restore") and not self.state.get("restored_needs_cutover"):
            self.state["restored_writers_released"] = True

    def stage_sq_configuration(self):
        if not self.state.get("sq_new_database"):
            return
        command = [sys.executable, str(self.source / "services/sq-music/bootstrap.py"), "configure", "--new-database", "--env-file", str(self.env_file)]
        for attempt in range(60):
            result = self.run(command, "sq-schema-configure", timeout=30, allow_failure=True)
            if not result.returncode:
                self.compose("restart", "sqmusic-server", label="reload-sq-config")
                return
            time.sleep(4)
        raise RuntimeError("SQ schema did not become ready; resume keeps the original database and credentials")

    def stage_activation(self):
        values = read_env(self.env_file)
        held = bool(self.state.get("restored_needs_cutover"))
        # On resume the services stage may already be complete. Release the
        # previously held queues only after this invocation approved cutover.
        if self.state.get("restore") and not held and not self.state.get("restored_writers_released"):
            self.compose("up", "-d", "--no-deps", *RESTORE_QUEUE_SERVICES, label="activate-restored-queue-services", timeout=600)
            self.state["restored_writers_released"] = True
        telegram = bool(values.get("TELEGRAM_BOT_TOKEN"))
        telegram_allowed = bool(values.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip())
        synology = values.get("SYNOLOGY_CHAT_ENABLED", "false").lower() in {"true", "1", "yes", "on"}
        synology_complete = bool(values.get("SYNOLOGY_CHAT_OUTGOING_TOKEN") and values.get("SYNOLOGY_CHAT_ALLOWED_USER_IDS", "").strip() and values.get("SYNOLOGY_CHAT_INCOMING_WEBHOOK_URL"))
        unsafe_entry = (telegram and not telegram_allowed) or (synology and not synology_complete)
        has_entry = not unsafe_entry and ((telegram and telegram_allowed) or (synology and synology_complete))
        start_bot = values.get("START_BOT", "true").lower() in {"true", "1", "yes", "on"}
        statuses = self.container_status()
        wrapper_ready = any(row.get("Service") == "wrapper" and row.get("Health") == "healthy" for row in statuses)
        self.state["pending"] = []
        if telegram and not telegram_allowed:
            self.state["pending"].append("configure a Telegram chat allowlist before enabling the bot")
        if synology and not synology_complete:
            self.state["pending"].append("configure Synology outgoing token, user allowlist and incoming reply webhook")
        if held:
            self.state["pending"].append("stop the old stack's task writers before explicitly approving START_BOT=true in this invocation's private config; restored Bot, LX/SQ apps and lyrics workers remain stopped")
        elif not start_bot:
            self.state["pending"].append("START_BOT is disabled in private configuration")
        if not has_entry:
            self.state["pending"].append("provide a private Telegram token or Synology Chat credentials")
        if not wrapper_ready:
            self.state["pending"].append("complete Apple Music wrapper login and wait for its health check")
        self.state["pending"] += ["complete YouTube/Bilibili browser login as needed", "configure authorized LX music sources"]
        if self.state.get("restore", {}).get("omitted_sources"):
            self.state["pending"].append("copy the media files omitted from the migration bundle separately")
        if not held and start_bot and has_entry and wrapper_ready:
            self.compose("up", "-d", "--no-deps", "bot", "wrapper-watchdog", "bot-watchdog", label="activate-bot-and-watchdogs")
            self.state["business_status"] = "configured"
        else:
            self.state["business_status"] = "needs_configuration"
            stopped = RESTORE_TASK_WRITERS if held else ("bot", "bot-watchdog", "wrapper-watchdog")
            if any(row.get("Service") in stopped and row.get("State") == "running" for row in statuses):
                self.compose("stop", *stopped, label="hold-restored-task-writers" if held else "hold-unconfigured-bot")

    def stage_verification(self):
        rows = self.container_status()
        by_service = {row.get("Service"): row for row in rows}
        expected = {"legacy-wrapper", "wrapper", "youtube-pot", "lxserver", "sq-mysql", "sq-neteasecloudmusicapi", "sqmusic-server",
                    "word-lyrics-resolver", "word-lyrics-nas-aligner"}
        if self.state.get("restored_needs_cutover"):
            expected.difference_update(RESTORE_TASK_WRITERS)
            if any(by_service.get(name, {}).get("State") == "running" for name in RESTORE_TASK_WRITERS):
                raise RuntimeError("restored task writers must remain stopped until cutover approval")
        if self.media:
            expected.update({"navidrome", "emby", "lrcapi"})
        if self.state.get("business_status") == "configured":
            expected.update({"bot", "wrapper-watchdog", "bot-watchdog"})
        failures = sorted(service for service in expected if by_service.get(service, {}).get("State") != "running")
        if failures:
            raise RuntimeError("services did not remain running: " + ", ".join(failures))
        self.state["infrastructure_ready"] = all(by_service[service].get("Health") not in {"unhealthy", "starting"} for service in expected)

    def container_status(self):
        if not self.descriptor:
            return []
        result = self.compose("ps", "--all", "--format", "json", label="container-status", timeout=30, allow_failure=True)
        if result.returncode:
            return []
        text = (result.stdout or "").strip()
        if not text:
            return []
        rows = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines()]
        return [{key: row.get(key) for key in ("Service", "State", "Health", "ExitCode")} for row in rows]

    def status(self):
        return {"schema": SCHEMA, "installed": self.state.get("status") == "installed",
                "status": self.state.get("status", "not_installed"), "business_status": self.state.get("business_status", "needs_configuration"),
                "configured": self.state.get("business_status") == "configured",
                "infrastructure_ready": bool(self.state.get("infrastructure_ready")),
                "readiness_scope": "real music/video download and account acceptance remain separate",
                "ready": False, "pending": self.state.get("pending", []), "failed_stage": self.state.get("failed_stage"),
                "completed": self.state.get("completed", []), "project_name": self.project,
                "containers": self.container_status() if self.descriptor else []}


def main(argv=None):
    if sys.version_info < (3, 8):
        print("Host Python 3.8 or newer is required.", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "resume", "status", "doctor"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--config", type=Path)
        command.add_argument("--restore", type=Path)
        command.add_argument("--with-media-servers", action="store_true")
        command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        installer = Installer(args.root, args.config, args.restore, args.with_media_servers, args.dry_run)
        if args.command == "doctor":
            if args.dry_run:
                result = {"dry_run": True, "checks": ["linux", "x86_64", "kvm", "docker", "compose", "disk", "ports", "ownership"]}
            else:
                result = {"host": installer.preflight(), "deployment": installer.status()}
                if installer.descriptor:
                    installer.stage_verification()
                    result["deployment"]["infrastructure_ready"] = installer.state.get("infrastructure_ready", False)
        elif args.command == "status":
            result = installer.status()
        else:
            # Activation is intentionally re-evaluated after the operator adds
            # credentials/logs into the persistent browsers or wrapper.
            installer.state["completed"] = [stage for stage in installer.state.get("completed", []) if stage != "activation"]
            result = installer.install()
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print("Installation incomplete: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
