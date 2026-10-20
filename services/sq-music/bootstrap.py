"""Prepare private SQ credentials and configure a NEW isolated SQ database.

Run from the repository root. Credentials are never printed or passed in
host command arguments. This helper never starts/stops services or downloads.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess

SERVICE_DIR = Path(__file__).resolve().parent
REPO_ROOT = SERVICE_DIR.parents[1]


def read_env(path: Path) -> tuple[str, dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise SystemExit("SQ bootstrap requires single-line .env values")
            value = value[1:-1]
        values[key.strip()] = value
    return text, values


def absolute_path(values: dict[str, str], key: str) -> Path:
    raw = values.get(key, "")
    path = Path(raw)
    if not raw or not path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"Set an absolute safe {key} in the root .env")
    if path == Path(path.anchor) or (key == "SQ_AUTH_PATH" and path.parent == Path(path.anchor)):
        raise SystemExit(f"{key} must use a dedicated directory below the filesystem root")
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise SystemExit(f"{key} must not use a symlink")
        current = current.parent
    return path


def read_auth(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise SystemExit("SQ auth file must not be a symlink")
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SystemExit("SQ auth file is missing or invalid; no contents printed") from None
    if not isinstance(auth, dict) or any(
        not isinstance(auth.get(key), str) or not auth[key].strip()
        for key in ("username", "password")
    ):
        raise SystemExit("SQ auth must contain nonempty username/password strings")
    return {key: auth[key] for key in ("username", "password")}


def prepare(env_file: Path) -> None:
    original, values = read_env(env_file)
    staging = absolute_path(values, "SQ_STAGING_PATH")
    database = absolute_path(values, "SQ_MYSQL_DATA_PATH")
    auth_path = absolute_path(values, "SQ_AUTH_PATH")
    roots = (staging, database, auth_path.parent)
    for index, left in enumerate(roots):
        for right in roots[index + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise SystemExit("SQ staging, database and secret directories must be separate and non-overlapping")
    if values.get("MUSIC_PATH"):
        music = Path(values["MUSIC_PATH"]).expanduser().resolve()
        for directory in roots:
            actual = directory.resolve()
            if actual == music or actual in music.parents or music in actual.parents:
                raise SystemExit("SQ private state and staging must be separate from the public MUSIC_PATH")
    needs_secret = any(not values.get(key) or values[key].startswith(("GENERATE_", "REPLACE_"))
                       for key in ("SQ_DB_PASSWORD", "SQ_DB_ROOT_PASSWORD", "SQ_JWT_SECRET"))
    if database.exists() and any(database.iterdir()) and (needs_secret or not auth_path.exists()):
        raise SystemExit("Existing SQ database found; restore its matching secrets instead of generating new ones")
    staging.mkdir(parents=True, exist_ok=True)
    database.mkdir(parents=True, exist_ok=True)
    auth_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(auth_path.parent, 0o700)
    if auth_path.exists():
        read_auth(auth_path)
        os.chmod(auth_path, 0o600)
    else:
        descriptor = os.open(auth_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"username": "sqbot", "password": secrets.token_urlsafe(36)}, handle)
            handle.write("\n")
    replacements = {}
    for key in ("SQ_DB_PASSWORD", "SQ_DB_ROOT_PASSWORD", "SQ_JWT_SECRET"):
        value = values.get(key, "")
        if not value or value.startswith(("GENERATE_", "REPLACE_")):
            replacements[key] = secrets.token_hex(48 if key == "SQ_JWT_SECRET" else 24)
    lines = []
    for line in original.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        lines.append(key + "=" + replacements.pop(key) if key in replacements else line)
    lines.extend(key + "=" + value for key, value in replacements.items())
    # Never replace runtime credentials on subsequent runs.
    os.chmod(env_file, 0o600)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Private SQ directories and credentials prepared; no secret values printed.")


def sql_string(value: str) -> str:
    # MySQL text literal without quoting or escaping user-provided credentials.
    return "CONVERT(0x" + value.encode("utf-8").hex() + " USING utf8mb4)"


def configure(env_file: Path, *, confirmed_new_database: bool) -> None:
    if not confirmed_new_database:
        raise SystemExit("Use --new-database only for this new isolated SQ database")
    _, values = read_env(env_file)
    auth = read_auth(absolute_path(values, "SQ_AUTH_PATH"))
    sql = (SERVICE_DIR / "bootstrap-config.sql").read_text(encoding="utf-8")
    for key, field in (("system.login.account", "username"), ("system.login.password", "password")):
        sql += "\nUPDATE sq_config SET config_value=" + sql_string(auth[field])
        sql += " WHERE config_key='" + key + "';\n"
    command = ["docker", "compose", "--env-file", str(env_file),
               "-f", str(REPO_ROOT / "compose.yaml"), "-f", str(SERVICE_DIR / "compose.yaml"),
               "--profile", "sq", "exec", "-T", "sq-mysql", "sh", "-c",
               'export MYSQL_PWD="$MYSQL_PASSWORD"; exec mysql --batch -usqmusic sqmusicv3']
    try:
        required = ("system.login.account", "system.login.password", "system.download.num",
                    "system.download.path", "system.download.file.template",
                    "system.download.file.audio.format", "system.download.timeout.retry.num",
                    "plug.netease.baseurl", "plug.netease.extend.download")
        preflight = "SELECT COUNT(DISTINCT config_key) FROM sq_config WHERE config_key IN ("
        preflight += ",".join("'" + key + "'" for key in required) + ");\n"
        check = subprocess.run(command, cwd=REPO_ROOT, input=preflight.encode("utf-8"),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if check.returncode or check.stdout.decode().splitlines()[-1:] != [str(len(required))]:
            raise SystemExit("SQ schema is not the audited version or not ready; no changes applied")
        result = subprocess.run(command, cwd=REPO_ROOT, input=sql.encode("utf-8"),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    except OSError:
        raise SystemExit("Could not run Docker Compose; no secret values printed") from None
    if result.returncode:
        raise SystemExit("SQ configuration failed; check service/schema readiness privately")
    print("New SQ database configured. Recreate/restart only sqmusic-server to reload its cache.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "configure"))
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--new-database", action="store_true")
    args = parser.parse_args()
    env_file = args.env_file.expanduser().resolve()
    if args.action == "prepare":
        prepare(env_file)
    else:
        configure(env_file, confirmed_new_database=args.new_database)


if __name__ == "__main__":
    main()
