"""Private, offline snapshots and verified restores; never starts or stops services.

The deployment descriptor is private metadata, not an instruction to discover or
control unrelated containers. Docker is queried only for explicitly selected
Compose projects. Restore never executes Docker commands or extracts arbitrary tar
members, and never edits a live database or the installer's private.env.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any

from deploy.environment import command_environment

SCHEMA = 1
FORMAT = "amdl-private-snapshot"
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
RUNTIME_TARGETS = {"/var/run/docker.sock", "/run/docker.sock"}
MYSQL_DATA_TARGET = "/var/lib/mysql"
MYSQL_SOCKET_ALIAS = "/var/run/mysqld/mysqld.sock"
# Official 5.7.44 source: docker-library/mysql commit
# 9678ed1d27794ae9529c43b4411e30f981ce39ea, 5.7/Dockerfile.oracle
# lines 74-79 and 5.7/docker-entrypoint.sh mysql_socket_fix(). The entrypoint
# recreates this transient alias; it is not a database or account-state file.
MEDIA_TARGETS = {"/music", "/media", "/library", "/youtube-music", "/youtube-video",
                 "/bilibili-music", "/bilibili-video", "/sq-staging", "/server/music/_open"}
MANIFEST_LIMIT = 64 * 1024 * 1024


class MigrationError(RuntimeError):
    pass


def _run(command: list[str]) -> str:
    try:
        timeout = 7200 if command[:3] == ["docker", "image", "save"] else 180
        completed = subprocess.run(command, check=True, text=True, capture_output=True, timeout=timeout,
                                   env=command_environment(command))
    except (OSError, subprocess.SubprocessError):
        raise MigrationError("Docker metadata operation failed; inspect private local diagnostics.") from None
    return completed.stdout


def _json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MANIFEST_LIMIT:
        raise MigrationError("Missing, oversized, or symlinked metadata file.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise MigrationError("Invalid JSON metadata.") from None
    if not isinstance(value, dict):
        raise MigrationError("Metadata must be a JSON object.")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise MigrationError("Unsafe archive or configuration path.")
    parts = value.rstrip("/").split("/")
    if any(part in {"", ".", ".."} or re.match(r"^[A-Za-z]:", part) for part in parts) or value.startswith("/"):
        raise MigrationError("Unsafe archive or configuration path.")
    return PurePosixPath(*parts)


def _local_child(root: Path, value: str) -> Path:
    if value == ".":
        return root
    relative = _safe_relative(value)
    result = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise MigrationError("Configuration paths may not cross symlinks.")
    if not _within(result.resolve(), root.resolve()):
        raise MigrationError("Configuration path leaves its selected deployment.")
    return result


def _projects(project_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    descriptor = _json(project_dir / "deployment.json")
    if descriptor.get("schema") != SCHEMA:
        raise MigrationError("Unsupported deployment descriptor schema.")
    specifications = descriptor.get("projects") or [descriptor]
    if not isinstance(specifications, list) or not specifications:
        raise MigrationError("Select at least one Compose project.")
    result, names, identifiers = [], set(), set()
    for index, original in enumerate(specifications):
        if not isinstance(original, dict):
            raise MigrationError("Invalid project descriptor.")
        spec = dict(original)
        identifier = spec.get("id", "main" if len(specifications) == 1 else f"project-{index}")
        name = spec.get("project_name")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or identifier in identifiers:
            raise MigrationError("Project identifiers must be unique safe names.")
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or name in names:
            raise MigrationError("Compose project names must be explicit and unique.")
        base = Path(spec.get("project_dir", project_dir))
        base = (project_dir / base).resolve() if not base.is_absolute() else base.resolve()
        source = _local_child(base, spec.get("source_dir", "source"))
        environment = _local_child(base, spec.get("env_file", "config/private.env"))
        files = spec.get("compose_files")
        if not isinstance(files, list) or not files or not all(isinstance(item, str) for item in files):
            raise MigrationError("Explicit Compose files are required for every project.")
        base_compose_files = [_local_child(source, value) for value in files]
        extras = spec.get("extra_compose_files", [])
        if not isinstance(extras, list) or not all(isinstance(item, str) for item in extras):
            raise MigrationError("Invalid deployment overlay list.")
        for value in extras:
            if _safe_relative(value).parts[0] not in {"source", "config"}:
                raise MigrationError("Deployment overlays must be under source/ or config/.")
        extra_compose_files = [_local_child(base, value) for value in extras]
        compose_files = base_compose_files + extra_compose_files
        if not environment.is_file() or any(not item.is_file() for item in compose_files):
            raise MigrationError("A selected environment or Compose source file is missing.")
        profiles = spec.get("profiles", [])
        if not isinstance(profiles, list) or any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value) for value in profiles):
            raise MigrationError("Invalid Compose profile list.")
        spec.update(id=identifier, project_name=name, project_dir=base, source_path=source,
                    environment_path=environment, compose_paths=compose_files, profiles=profiles,
                    base_compose_paths=base_compose_files, extra_compose_paths=extra_compose_files,
                    extra_compose_files=extras)
        result.append(spec)
        identifiers.add(identifier)
        names.add(name)
    return descriptor, result


def _compose_config(project: dict[str, Any]) -> dict[str, Any]:
    command = ["docker", "compose", "--project-directory", str(project["source_path"]),
               "--env-file", str(project["environment_path"]), "--project-name", project["project_name"]]
    for path in project["compose_paths"]:
        command += ["-f", str(path)]
    for profile in project["profiles"]:
        command += ["--profile", profile]
    config = json.loads(_run(command + ["config", "--format", "json"]))
    if not isinstance(config.get("services"), dict) or not config["services"]:
        raise MigrationError("Selected project has no resolved services.")
    return config


def _containers() -> list[dict[str, Any]]:
    ids = _run(["docker", "ps", "--all", "--quiet"]).split()
    if not ids:
        return []
    payload = json.loads(_run(["docker", "inspect", *ids]))
    result = []
    for row in payload:
        labels = row.get("Config", {}).get("Labels") or {}
        result.append({"id": row["Id"], "project": labels.get("com.docker.compose.project", ""),
                       "service": labels.get("com.docker.compose.service", ""),
                       "running": bool(row.get("State", {}).get("Running")), "image_id": row.get("Image", ""),
                       "image_ref": row.get("Config", {}).get("Image", ""),
                       "working_dir": labels.get("com.docker.compose.project.working_dir", ""),
                       "config_files": labels.get("com.docker.compose.project.config_files", ""),
                       "mounts": [{"type": item.get("Type"), "source": item.get("Source", ""),
                                   "target": item.get("Destination", ""), "name": item.get("Name", ""),
                                   "read_only": not bool(item.get("RW", True))} for item in row.get("Mounts", [])]})
    return result


def inventory(project_names: list[str] | None = None) -> dict[str, Any]:
    """Return read-only, secret-free metadata for reviewing a legacy migration plan."""
    if project_names and any(not IDENTIFIER.fullmatch(value) for value in project_names):
        raise MigrationError("Invalid selected project name.")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in _containers():
        name = row["project"]
        if name and (not project_names or name in project_names):
            grouped.setdefault(name, []).append(row)
    if project_names and set(project_names) - grouped.keys():
        raise MigrationError("A selected project has no container evidence; review its exact name.")
    return {"schema": SCHEMA, "kind": "read-only-compose-inventory", "projects": grouped,
            "requires_review": True, "service_changes": False,
            "note": "Private host paths only; no environment values, cookies, or container actions."}


def _volume_path(config: dict[str, Any], logical_name: str) -> tuple[str, Path]:
    entry = config.get("volumes", {}).get(logical_name) or {}
    name = entry.get("name")
    if not name:
        raise MigrationError("Named volume has no resolved Docker volume name.")
    rows = json.loads(_run(["docker", "volume", "inspect", name]))
    if len(rows) != 1 or not rows[0].get("Mountpoint"):
        raise MigrationError("Named volume has no inspectable local data source.")
    return name, Path(rows[0]["Mountpoint"])


def _offline(projects: list[dict[str, Any]], configs: dict[str, dict[str, Any]],
             sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _containers()
    selected = {project["project_name"]: project for project in projects}
    for name, project in selected.items():
        observed = [row for row in rows if row["project"] == name]
        if not observed:
            raise MigrationError("A selected project has no container evidence; cannot verify offline state.")
        if any(row["service"] not in configs[project["id"]]["services"] for row in observed):
            raise MigrationError("Selected project contains services absent from its supplied Compose files.")
        if any(row["running"] for row in observed):
            raise MigrationError("Snapshot requires every selected project to be stopped; no containers were changed.")
    included = [Path(item["path"]) for item in sources if item["included"]]
    for row in rows:
        if not row["running"] or row["project"] in selected:
            continue
        for mount in row["mounts"]:
            if mount["read_only"] or not mount["source"]:
                continue
            source = Path(mount["source"]).resolve()
            if any(_within(source, root) or _within(root, source) for root in included):
                raise MigrationError("Another running container can write selected snapshot data; stop/review it separately.")
    return [row for row in rows if row["project"] in selected]


def _collect_mounts(projects: list[dict[str, Any]], configs: dict[str, dict[str, Any]],
                    include_media: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sources: dict[str, dict[str, Any]] = {}
    mounts = []
    for project in projects:
        config = configs[project["id"]]
        media_paths = [Path(value).resolve() if Path(value).is_absolute()
                       else (project["project_dir"] / value).resolve() for value in project.get("media_paths", [])]
        for service, settings in config["services"].items():
            for mount in settings.get("volumes", []):
                kind, target = mount.get("type"), mount.get("target")
                record = {"project_id": project["id"], "service": service, "target": target,
                          "type": kind, "read_only": bool(mount.get("read_only"))}
                if kind == "tmpfs" or target in RUNTIME_TARGETS:
                    mounts.append({**record, "included": False, "reason": "ephemeral-runtime"})
                    continue
                if kind not in {"bind", "volume"} or not mount.get("source"):
                    raise MigrationError("Anonymous or unsupported persistent mounts require an explicit migration plan.")
                if kind == "volume":
                    name, source = _volume_path(config, mount["source"])
                    record["volume_name"] = name
                else:
                    source = Path(mount["source"])
                if not source.is_absolute() or source.is_symlink():
                    raise MigrationError("Persistent mount roots must be absolute, non-symlink local paths.")
                source = source.resolve()
                if source == Path(source.anchor) or not source.exists():
                    raise MigrationError("A persistent source is missing or is a filesystem root; snapshot is incomplete.")
                metadata = source.lstat()
                if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                    raise MigrationError("Persistent sources must be ordinary directories or files.")
                is_media = target in MEDIA_TARGETS or any(_within(source, value) for value in media_paths)
                included = include_media or not is_media
                key = str(source)
                if key not in sources:
                    sources[key] = {"id": f"s{len(sources):04d}", "path": key, "included": included,
                                    "kind": "directory" if source.is_dir() else "file", "media": is_media,
                                    "contains_media": is_media}
                else:
                    sources[key]["included"] |= included
                    sources[key]["media"] &= is_media
                    sources[key]["contains_media"] |= is_media
                mounts.append({**record, "source": key, "source_id": sources[key]["id"],
                               "source_kind": "directory" if source.is_dir() else "file", "media": is_media})
    # Compose intentionally shares a whole music root with the bot and a child
    # with LX. Archive the outer root once; all aliases keep relative subpaths.
    originals = list(sources.values())
    roots = [dict(item) for item in originals if not any(
        other["path"] != item["path"] and _within(Path(item["path"]), Path(other["path"]))
        for other in originals)]
    for index, root in enumerate(roots):
        children = [item for item in originals if _within(Path(item["path"]), Path(root["path"]))]
        root.update(id=f"s{index:04d}", included=any(item["included"] for item in children),
                    media=all(item["media"] for item in children),
                    contains_media=any(item["contains_media"] for item in children))
    for mount in mounts:
        if "source_id" in mount:
            root = next(item for item in roots if _within(Path(mount["source"]), Path(item["path"])))
            relative = Path(mount["source"]).relative_to(Path(root["path"]))
            mount.update(source_id=root["id"], source_relative=relative.as_posix(),
                         included=root["included"], reason="included" if root["included"] else "media-not-requested")
            if mount["media"] and not include_media and root["included"]:
                mount["media_retained_with_parent"] = True
    return roots, mounts


def _hash_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise MigrationError("Expected an ordinary file while hashing.")
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _mysql_socket_metadata(path: Path) -> dict[str, Any] | None:
    """Recognize only the official MySQL data-root socket, without following it."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        link = os.readlink(path)
        if link != MYSQL_SOCKET_ALIAS:
            return None
        kind = "symlink"
    elif stat.S_ISSOCK(metadata.st_mode):
        kind, link = "socket", None
    else:
        return None
    return {"kind": kind, "link": link, "mode": stat.S_IMODE(metadata.st_mode) & 0o777,
            "uid": metadata.st_uid, "gid": metadata.st_gid, "mtime_ns": metadata.st_mtime_ns,
            "device": metadata.st_dev, "inode": metadata.st_ino}


def _mysql_runtime_omissions(mounts: list[dict[str, Any]], rows: list[dict[str, Any]],
                             projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Call only after _offline. Require actual stopped-container mount evidence,
    # rather than treating any file named mysql.sock as an expendable socket.
    names = {project["id"]: project["project_name"] for project in projects}
    result = {}
    for mount in mounts:
        if (mount.get("target") != MYSQL_DATA_TARGET or not mount.get("included")
                or mount.get("source_kind") != "directory"):
            continue
        observed = [row for row in rows if row["project"] == names[mount["project_id"]]
                    and row["service"] == mount["service"] and not row["running"]]
        if not any(item.get("target") == MYSQL_DATA_TARGET and item.get("source") == mount["source"]
                   for row in observed for item in row["mounts"]):
            continue
        path = Path(mount["source"]) / "mysql.sock"
        metadata = _mysql_socket_metadata(path)
        if metadata is None:
            continue
        relative = mount.get("source_relative", ".")
        archive_path = f"data/{mount['source_id']}/" + (relative + "/" if relative != "." else "") + "mysql.sock"
        result.setdefault(archive_path, {"path": archive_path, "source": str(path),
                                        "source_id": mount["source_id"], "project_id": mount["project_id"],
                                        "service": mount["service"], "target": MYSQL_DATA_TARGET + "/mysql.sock",
                                        "reason": "mysql-runtime-socket", **metadata})
    return list(result.values())


def _scan(root: Path, prefix: str, runtime_omissions: dict[str, dict[str, Any]] | None = None,
          omitted_seen: set[str] | None = None) -> list[dict[str, Any]]:
    result = []
    def visit(path: Path, archive_path: str):
        if runtime_omissions and archive_path in runtime_omissions:
            expected = runtime_omissions[archive_path]
            actual = _mysql_socket_metadata(path)
            if actual is None or any(expected.get(key) != value for key, value in actual.items()):
                raise MigrationError("An omitted MySQL runtime socket changed while snapshotting.")
            if omitted_seen is not None:
                omitted_seen.add(archive_path)
            return
        metadata = path.lstat()
        record = {"path": archive_path, "mode": stat.S_IMODE(metadata.st_mode) & 0o777,
                  "uid": metadata.st_uid, "gid": metadata.st_gid, "mtime_ns": metadata.st_mtime_ns}
        if stat.S_ISDIR(metadata.st_mode):
            result.append({**record, "kind": "directory", "size": 0})
            with os.scandir(path) as iterator:
                names = sorted(item.name for item in iterator)
            for name in names:
                _safe_relative(name)
                visit(path / name, f"{archive_path}/{name}")
        elif stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise MigrationError("Hard-linked files need a separate reviewed migration plan.")
            digest, size = _hash_file(path)
            result.append({**record, "kind": "file", "sha256": digest, "size": size})
        elif stat.S_ISLNK(metadata.st_mode):
            destination = path.resolve(strict=True)
            if not root.is_dir() or not _within(destination, root.resolve()):
                raise MigrationError("External or dangling symlinks cannot be included.")
            link = os.path.relpath(destination, path.parent)
            result.append({**record, "kind": "symlink", "link": link, "size": 0})
        else:
            raise MigrationError("Device, socket, FIFO, or other special files cannot be included.")
    visit(root, prefix)
    return result


def _file_sources(project_dir: Path, projects: list[dict[str, Any]], sources: list[dict[str, Any]]):
    result = [(Path(source["path"]), f"data/{source['id']}") for source in sources if source["included"]]
    result.append((project_dir / "deployment.json", "config/deployment.json"))
    for project in projects:
        result.append((project["environment_path"], f"config/{project['id']}/private.env"))
        for relative, path in zip(project["compose_files"], project["base_compose_paths"]):
            result.append((path, f"config/{project['id']}/compose/{relative}"))
        for relative, path in zip(project["extra_compose_files"], project["extra_compose_paths"]):
            result.append((path, f"config/{project['id']}/overrides/{relative}"))
    return result


def _records(file_sources: list[tuple[Path, str]],
             runtime_omissions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    omissions = {item["path"]: item for item in runtime_omissions or []}
    seen = set()
    records = [record for source, prefix in file_sources for record in _scan(source, prefix, omissions, seen)]
    if seen != omissions.keys():
        raise MigrationError("An omitted MySQL runtime socket disappeared while snapshotting.")
    paths = [record["path"] for record in records]
    if len(paths) != len(set(paths)):
        raise MigrationError("Duplicate archive paths in snapshot coverage.")
    return records


def _write_payload(path: Path, records: list[dict[str, Any]], file_sources: list[tuple[Path, str]]) -> None:
    def source_for(name: str) -> Path:
        for root, prefix in file_sources:
            if name == prefix:
                return root
            if name.startswith(prefix + "/"):
                return root.joinpath(*PurePosixPath(name[len(prefix) + 1:]).parts)
        raise MigrationError("An archive entry has no selected source.")
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        for record in records:
            member = tarfile.TarInfo(record["path"])
            member.mode, member.uid, member.gid = record["mode"], record["uid"], record["gid"]
            member.mtime = record["mtime_ns"] / 1_000_000_000
            if record["kind"] == "directory":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            elif record["kind"] == "symlink":
                member.type, member.linkname = tarfile.SYMTYPE, record["link"]
                archive.addfile(member)
            else:
                member.size = record["size"]
                descriptor = os.open(source_for(member.name), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise MigrationError("A source file changed type while archiving.")
                    archive.addfile(member, handle)
    path.chmod(0o600)


def _project_metadata(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": project["id"], "project_name": project["project_name"],
             "compose_files": project["compose_files"], "profiles": project["profiles"],
             "extra_compose_files": project["extra_compose_files"],
             "managed_paths": project.get("managed_paths", {}),
             "source_dir": str(project["source_path"]), "env_file": f"config/{project['id']}/private.env"}
            for project in projects]


def snapshot(project_dir: Path, destination: Path, include_media: bool = False,
             include_images: bool = False) -> dict[str, Any]:
    """Snapshot selected, verifiably stopped projects; never stops production itself."""
    project_dir, destination = Path(project_dir).resolve(), Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise MigrationError("Snapshot destination must not exist.")
    destination = destination.resolve()
    if _within(destination, project_dir):
        raise MigrationError("Keep private snapshots outside the deployment directory.")
    descriptor, projects = _projects(project_dir)
    configs = {project["id"]: _compose_config(project) for project in projects}
    sources, mounts = _collect_mounts(projects, configs, include_media)
    if any(_within(destination.resolve(), Path(source["path"])) for source in sources):
        raise MigrationError("Snapshot destination is inside a selected data source.")
    rows = _offline(projects, configs, sources)
    runtime_omissions = _mysql_runtime_omissions(mounts, rows, projects)
    file_sources = _file_sources(project_dir, projects, sources)
    records = _records(file_sources, runtime_omissions)
    # Check again before creating a private bundle, not merely after it finishes.
    _offline(projects, configs, sources)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".amdl-snapshot-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        _write_payload(staging / "payload.tar", records, file_sources)
        image_metadata = [{"project": row["project"], "service": row["service"],
                           "image_id": row["image_id"], "image_ref": row["image_ref"]} for row in rows]
        image_archive = None
        if include_images:
            ids = sorted({row["image_id"] for row in rows if row["image_id"]})
            if not ids:
                raise MigrationError("No inspected image IDs are available for image export.")
            image_file = staging / "images.tar"
            _run(["docker", "image", "save", "--output", str(image_file), *ids])
            image_file.chmod(0o600)
            image_hash, image_size = _hash_file(image_file)
            image_archive = {"name": "images.tar", "sha256": image_hash, "size": image_size}
        payload_hash, payload_size = _hash_file(staging / "payload.tar")
        manifest = {"schema": SCHEMA, "format": FORMAT, "complete": True,
                    "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "offline": True,
                    "projects": _project_metadata(projects), "sources": sources, "mounts": mounts,
                    "files": records, "images": image_metadata, "image_archive": image_archive,
                    "include_media": include_media, "include_images": include_images,
                    "archive": {"name": "payload.tar", "sha256": payload_hash, "size": payload_size},
                    "coverage": {"all_persistent_mounts_accounted_for": True,
                                 "omitted_mounts": [item for item in mounts if not item["included"]],
                                 "runtime_omissions": runtime_omissions,
                                 "media_retained_with_parent": [item for item in mounts if item.get("media_retained_with_parent")]},
                    "queue_policy": "offline byte-preserving snapshot; no task status normalization",
                    "ownership_policy": "original uid/gid recorded; restore does not automatically chown"}
        _validate_payload(staging / "payload.tar", manifest)
        if records != _records(file_sources, runtime_omissions):
            raise MigrationError("Source data changed while snapshotting; bundle was not published.")
        _offline(projects, configs, sources)
        _write_json(staging / "manifest.json", manifest)
        os.replace(staging, destination)
    return manifest


def _manifest_records(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = manifest.get("files")
    if not isinstance(records, list):
        raise MigrationError("Missing file integrity ledger.")
    result = {}
    for item in records:
        if not isinstance(item, dict):
            raise MigrationError("Invalid file integrity record.")
        path = str(_safe_relative(item.get("path")))
        if path.split("/")[0] not in {"config", "data"} or path in result:
            raise MigrationError("Unexpected or duplicate payload path.")
        if item.get("kind") not in {"file", "directory", "symlink"}:
            raise MigrationError("Unsupported payload entry type.")
        if not isinstance(item.get("size"), int) or item["size"] < 0:
            raise MigrationError("Invalid payload entry size.")
        if item["kind"] == "file" and not DIGEST.fullmatch(item.get("sha256", "")):
            raise MigrationError("Missing SHA-256 for a payload file.")
        mode = item.get("mode")
        if not isinstance(mode, int) or mode < 0 or mode > 0o777:
            raise MigrationError("Unsafe payload permissions.")
        if not isinstance(item.get("mtime_ns"), int) or abs(item["mtime_ns"]) > (1 << 63) - 1:
            raise MigrationError("Invalid payload timestamp.")
        if item["kind"] != "file" and item["size"] != 0:
            raise MigrationError("Non-file payload entries must not contain bytes.")
        result[path] = item
    for name, item in result.items():
        parts = PurePosixPath(name).parts
        for length in range(1, len(parts)):
            parent = result.get(str(PurePosixPath(*parts[:length])))
            if parent and parent["kind"] != "directory":
                raise MigrationError("Payload path crosses a file or symlink.")
        if item["kind"] == "symlink":
            if len(parts) < 3 or parts[0] != "data":
                raise MigrationError("Symlinks are allowed only inside a data source.")
            link = item.get("link")
            if not isinstance(link, str) or "\\" in link or "\x00" in link or link.startswith("/"):
                raise MigrationError("Unsafe symlink target.")
            normalized = os.path.normpath(str(PurePosixPath(name).parent / link)).replace(os.sep, "/")
            if normalized != f"data/{parts[1]}" and not normalized.startswith(f"data/{parts[1]}/"):
                raise MigrationError("Symlink target leaves its data source.")
            if normalized not in result:
                raise MigrationError("Symlink target is absent from the verified payload.")
            visited = {name}
            while result[normalized]["kind"] == "symlink":
                if normalized in visited:
                    raise MigrationError("Cyclic symlinks are forbidden.")
                visited.add(normalized)
                target = result[normalized].get("link", "")
                normalized = os.path.normpath(str(PurePosixPath(normalized).parent / target)).replace(os.sep, "/")
                if normalized not in result or not normalized.startswith(f"data/{parts[1]}/"):
                    if normalized != f"data/{parts[1]}":
                        raise MigrationError("Invalid indirect symlink target.")
    return result


def _validate_coverage(manifest: dict[str, Any], records: dict[str, dict[str, Any]]) -> None:
    projects, sources, mounts = manifest.get("projects"), manifest.get("sources"), manifest.get("mounts")
    if not isinstance(projects, list) or not projects or not isinstance(sources, list) or not isinstance(mounts, list):
        raise MigrationError("Incomplete project or data-source coverage ledger.")
    project_ids = set()
    for project in projects:
        identifier, name = project.get("id"), project.get("project_name")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or identifier in project_ids:
            raise MigrationError("Invalid project coverage identifier.")
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise MigrationError("Invalid Compose project coverage name.")
        project_ids.add(identifier)
        expected_env = f"config/{identifier}/private.env"
        if project.get("env_file") != expected_env or records.get(expected_env, {}).get("kind") != "file":
            raise MigrationError("A selected project's private environment is absent from the payload.")
        compose_files = project.get("compose_files")
        if not isinstance(compose_files, list) or not compose_files:
            raise MigrationError("Missing selected Compose configuration coverage.")
        for filename in compose_files:
            relative = str(_safe_relative(filename))
            if records.get(f"config/{identifier}/compose/{relative}", {}).get("kind") != "file":
                raise MigrationError("A selected Compose file is absent from the payload.")
        extras = project.get("extra_compose_files", [])
        if not isinstance(extras, list):
            raise MigrationError("Invalid selected overlay coverage.")
        for filename in extras:
            relative = str(_safe_relative(filename))
            if records.get(f"config/{identifier}/overrides/{relative}", {}).get("kind") != "file":
                raise MigrationError("A selected deployment overlay is absent from the payload.")
    if records.get("config/deployment.json", {}).get("kind") != "file":
        raise MigrationError("Deployment descriptor is absent from the payload.")
    by_id, source_paths = {}, set()
    for source in sources:
        identifier, path = source.get("id"), source.get("path")
        if (not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier) or identifier in by_id
                or not isinstance(path, str) or not Path(path).is_absolute() or path in source_paths
                or source.get("kind") not in {"directory", "file"} or not isinstance(source.get("included"), bool)):
            raise MigrationError("Invalid persistent-source coverage record.")
        if Path(path) == Path(Path(path).anchor):
            raise MigrationError("Filesystem roots cannot be persistent source records.")
        if not source["included"] and (source.get("media") is not True or manifest.get("include_media") is True):
            raise MigrationError("Only explicitly omitted media sources may be absent from a snapshot.")
        by_id[identifier] = source
        source_paths.add(path)
        entry = records.get(f"data/{identifier}")
        if source["included"]:
            if not entry or entry["kind"] != source["kind"]:
                raise MigrationError("An included persistent source is absent from the payload.")
        elif entry:
            raise MigrationError("An omitted data source unexpectedly appears in the payload.")
    referenced = set()
    for mount in mounts:
        if mount.get("project_id") not in project_ids or not isinstance(mount.get("service"), str):
            raise MigrationError("A mount is not owned by a selected project.")
        target = mount.get("target")
        if not isinstance(target, str) or not target.startswith("/") or ".." in PurePosixPath(target).parts:
            raise MigrationError("Unsafe container target in a mount record.")
        identifier = mount.get("source_id")
        if identifier is None:
            if mount.get("reason") != "ephemeral-runtime" or mount.get("included") is not False:
                raise MigrationError("Unaccounted persistent mount.")
            continue
        source = by_id.get(identifier)
        relative = mount.get("source_relative", ".")
        if relative != ".":
            relative = str(_safe_relative(relative))
        expected_source = str(Path(source["path"]) / relative) if source else None
        if not source or mount.get("source") != expected_source or mount.get("included") != source["included"]:
            raise MigrationError("Mount/source coverage mismatch.")
        if mount.get("reason") != ("included" if source["included"] else "media-not-requested"):
            raise MigrationError("Invalid mount inclusion reason.")
        if source["included"]:
            archive_path = f"data/{identifier}" + ("/" + relative if relative != "." else "")
            if records.get(archive_path, {}).get("kind") != mount.get("source_kind", source["kind"]):
                raise MigrationError("A mounted child source is absent from its containing archive root.")
        referenced.add(identifier)
    if referenced != by_id.keys():
        raise MigrationError("Unreferenced data source in the coverage ledger.")
    for name in records:
        parts = PurePosixPath(name).parts
        if parts[0] == "data" and (len(parts) < 2 or parts[1] not in by_id or not by_id[parts[1]]["included"]):
            raise MigrationError("Payload data has no included source declaration.")
        if parts[0] == "config" and name != "config/deployment.json" and (len(parts) < 2 or parts[1] not in project_ids):
            raise MigrationError("Payload configuration has no selected project declaration.")
    coverage = manifest.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("all_persistent_mounts_accounted_for") is not True:
        raise MigrationError("Snapshot does not declare complete persistent-source accounting.")
    if coverage.get("omitted_mounts") != [item for item in mounts if not item.get("included")]:
        raise MigrationError("Omitted-source coverage list differs from the mount ledger.")
    omissions = coverage.get("runtime_omissions", [])
    if not isinstance(omissions, list):
        raise MigrationError("Invalid runtime-omission coverage ledger.")
    seen = set()
    for item in omissions:
        if not isinstance(item, dict):
            raise MigrationError("Invalid runtime-omission record.")
        name = str(_safe_relative(item.get("path")))
        matching = [mount for mount in mounts if mount.get("project_id") == item.get("project_id")
                    and mount.get("service") == item.get("service") and mount.get("source_id") == item.get("source_id")
                    and mount.get("target") == MYSQL_DATA_TARGET and mount.get("source_kind") == "directory"
                    and mount.get("included") is True
                    and str(Path(mount["source"]) / "mysql.sock") == item.get("source")]
        expected_paths = {f"data/{mount['source_id']}/"
                          + (mount.get("source_relative", ".") + "/" if mount.get("source_relative", ".") != "." else "")
                          + "mysql.sock" for mount in matching}
        if (name in seen or name in records or name not in expected_paths
                or records.get(str(PurePosixPath(name).parent), {}).get("kind") != "directory"
                or item.get("reason") != "mysql-runtime-socket" or item.get("target") != MYSQL_DATA_TARGET + "/mysql.sock"
                or item.get("kind") not in {"symlink", "socket"}
                or item.get("link") != (MYSQL_SOCKET_ALIAS if item["kind"] == "symlink" else None)):
            raise MigrationError("Runtime omission is not the verified MySQL socket alias.")
        for key in ("mode", "uid", "gid", "mtime_ns", "device", "inode"):
            if not isinstance(item.get(key), int) or item[key] < 0:
                raise MigrationError("Invalid runtime-socket metadata.")
        seen.add(name)


def _validate_payload(path: Path, manifest: dict[str, Any]) -> None:
    expected = _manifest_records(manifest)
    _validate_coverage(manifest, expected)
    seen = set()
    try:
        with tarfile.open(path, "r:") as archive:
            for member in archive:
                name = str(_safe_relative(member.name))
                record = expected.get(name)
                if record is None or name in seen:
                    raise MigrationError("Archive contains an unlisted or duplicate member.")
                seen.add(name)
                if member.islnk() or member.isdev() or member.isfifo():
                    raise MigrationError("Hard links and special tar members are forbidden.")
                if record["kind"] == "directory":
                    if not member.isdir() or member.size:
                        raise MigrationError("Archive directory differs from its ledger.")
                elif record["kind"] == "symlink":
                    if not member.issym() or member.linkname != record["link"] or member.size:
                        raise MigrationError("Archive symlink differs from its ledger.")
                else:
                    if not member.isreg() or member.size != record["size"]:
                        raise MigrationError("Archive file differs from its ledger.")
                    digest = hashlib.sha256()
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise MigrationError("Archive file cannot be read.")
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                    if digest.hexdigest() != record["sha256"]:
                        raise MigrationError("A payload file failed SHA-256 verification.")
    except (tarfile.TarError, EOFError, OSError):
        raise MigrationError("Payload archive is invalid or unreadable.") from None
    if seen != expected.keys():
        raise MigrationError("Archive omits a required ledger entry.")


def _artifact(bundle: Path, metadata: dict[str, Any], expected_name: str) -> Path:
    if not isinstance(metadata, dict) or metadata.get("name") != expected_name:
        raise MigrationError("Invalid bundle artifact name.")
    path = bundle / expected_name
    if path.is_symlink() or not path.is_file() or not DIGEST.fullmatch(metadata.get("sha256", "")):
        raise MigrationError("Missing or unsafe bundle artifact.")
    digest, size = _hash_file(path)
    if digest != metadata["sha256"] or size != metadata.get("size"):
        raise MigrationError("Bundle artifact failed SHA-256 verification.")
    return path


def validate_bundle(bundle: Path) -> dict[str, Any]:
    bundle = Path(bundle)
    if bundle.is_symlink() or not bundle.is_dir():
        raise MigrationError("Bundle must be a real private snapshot directory.")
    manifest = _json(bundle / "manifest.json")
    if (manifest.get("schema") != SCHEMA or manifest.get("format") != FORMAT
            or manifest.get("complete") is not True or manifest.get("offline") is not True):
        raise MigrationError("Unsupported or incomplete snapshot bundle.")
    expected_names = {"manifest.json", "payload.tar"} | ({"images.tar"} if manifest.get("include_images") else set())
    if {item.name for item in bundle.iterdir()} != expected_names:
        raise MigrationError("Snapshot contains unlisted or missing bundle artifacts.")
    payload = _artifact(bundle, manifest.get("archive"), "payload.tar")
    _validate_payload(payload, manifest)
    if manifest.get("include_images") is True:
        _artifact(bundle, manifest.get("image_archive"), "images.tar")
    elif manifest.get("image_archive") is not None:
        raise MigrationError("Unexpected image archive declaration.")
    return manifest


def _empty_target(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise MigrationError("Restore destinations may not cross symlinks.")
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise MigrationError("Restore destinations must be new or empty directories.")


def _restore_result(manifest: dict[str, Any], target_root: Path, config_dir: Path) -> dict[str, Any]:
    source_map = _restored_source_paths(manifest, target_root)
    for source in manifest.get("sources", []):
        identifier = source.get("id")
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise MigrationError("Invalid restored source identifier.")
    mounts = []
    for item in manifest.get("mounts", []):
        record = dict(item)
        record["restored_path"] = source_map.get(item.get("source"))
        mounts.append(record)
    env_files = {project["id"]: str(config_dir / project["id"] / "private.env")
                 for project in manifest.get("projects", [])}
    return {"schema": SCHEMA, "projects": manifest.get("projects", []), "source_map": source_map,
            "mounts": mounts, "env_files": env_files, "media_included": manifest.get("include_media", False),
            "omitted_sources": [source for source in manifest.get("sources", []) if not source["included"]],
            "media_retained_with_parent": manifest.get("coverage", {}).get("media_retained_with_parent", []),
            "runtime_omissions": manifest.get("coverage", {}).get("runtime_omissions", []),
            "images_included": manifest.get("include_images", False), "images": manifest.get("images", []),
            "image_archive": str(target_root / "_images/images.tar") if manifest.get("include_images") else None,
            "ownership_policy": manifest.get("ownership_policy"),
            "queue_policy": manifest.get("queue_policy")}


def _restored_source_paths(manifest: dict[str, Any], target_root: Path) -> dict[str, str]:
    """Preserve declared parent/child layouts such as LX_DATA_PATH/data/config.js."""
    sources = manifest.get("sources", [])
    anchors = []
    for project in manifest.get("projects", []):
        managed = project.get("managed_paths", {})
        if not isinstance(managed, dict):
            raise MigrationError("Invalid managed-path metadata.")
        for key, value in managed.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
                    or not isinstance(value, str) or not Path(value).is_absolute()
                    or ".." in Path(value).parts or Path(value) == Path(Path(value).anchor)):
                raise MigrationError("Unsafe managed-path mapping.")
            anchors.append((Path(value), target_root / "managed" / project["id"] / key.lower()))
    result = {}
    for source in sources:
        path = Path(source["path"])
        candidates = [entry for entry in anchors if _within(path, entry[0])]
        if candidates:
            old_root, new_root = min(candidates, key=lambda entry: (len(entry[0].parts), str(entry[1])))
            result[source["path"]] = str(new_root / path.relative_to(old_root))
        else:
            result[source["path"]] = str(target_root / source["id"])
    # Parent aliases and known subpaths must resolve to the same preserved tree.
    for old, unused_destination in anchors:
        candidates = [entry for entry in anchors if _within(old, entry[0])
                      and any(_within(Path(source["path"]), entry[0]) for source in sources)]
        if candidates:
            old_root, new_root = min(candidates, key=lambda entry: (len(entry[0].parts), str(entry[1])))
            result[str(old)] = str(new_root / old.relative_to(old_root))
        else:
            for source in sources:
                root = Path(source["path"])
                if _within(old, root):
                    result[str(old)] = str(Path(result[source["path"]]) / old.relative_to(root))
                    break
    by_id = {source["id"]: source for source in sources}
    for mount in manifest.get("mounts", []):
        if "source_id" in mount:
            root = by_id[mount["source_id"]]
            result[mount["source"]] = str(Path(result[root["path"]]) / mount.get("source_relative", "."))
    return result


def restore(bundle: Path, target_root: Path, config_dir: Path, *, dry_run: bool = False,
            plan_validator=None) -> dict[str, Any]:
    """Verify the whole bundle, then write only new data/config destinations."""
    bundle, target_root, config_dir = Path(bundle).absolute(), Path(target_root).absolute(), Path(config_dir).absolute()
    manifest = validate_bundle(bundle)
    _empty_target(target_root)
    _empty_target(config_dir)
    bundle, target_root, config_dir = bundle.resolve(), target_root.resolve(), config_dir.resolve()
    if _within(target_root, config_dir) or _within(config_dir, target_root):
        raise MigrationError("Data and saved configuration destinations must not overlap.")
    if _within(target_root, bundle) or _within(config_dir, bundle):
        raise MigrationError("Restore targets must be outside the snapshot bundle.")
    result = _restore_result(manifest, target_root, config_dir)
    if plan_validator is not None:
        result = plan_validator(result)
        if not isinstance(result, dict):
            raise MigrationError("Restore plan validation did not return a mapping.")
    if dry_run:
        return result
    # Every input was checked before any destination directory is created.
    target_root.parent.mkdir(parents=True, exist_ok=True)
    config_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".amdl-restore-", dir=target_root.parent) as temporary:
        staging = Path(temporary)
        data_stage, config_stage = staging / "data", staging / "config"
        data_stage.mkdir(mode=0o700)
        config_stage.mkdir(mode=0o700)
        records = _manifest_records(manifest)
        source_by_id = {source["id"]: source for source in manifest["sources"]}
        locations = {}
        for name, record in records.items():
            parts = PurePosixPath(name).parts
            if parts[0] == "data":
                restored_source = Path(result["source_map"][source_by_id[parts[1]]["path"]])
                locations[name] = data_stage.joinpath(*restored_source.relative_to(target_root).parts, *parts[2:])
            else:
                locations[name] = config_stage.joinpath(*parts[1:])
        # No symlink is created until all regular files/directories are written.
        with tarfile.open(bundle / "payload.tar", "r:") as archive:
            for member in archive:
                name = str(_safe_relative(member.name))
                record, destination = records[name], locations[name]
                if record["kind"] == "symlink":
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if record["kind"] == "directory":
                    destination.mkdir(exist_ok=True, mode=0o700)
                    continue
                digest = hashlib.sha256()
                with destination.open("xb") as output:
                    stream = archive.extractfile(member)
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if digest.hexdigest() != record["sha256"]:
                    raise MigrationError("Payload changed after verification; restore was not published.")
                destination.chmod(0o600 if name.startswith("config/") else record["mode"])
                os.utime(destination, ns=(record["mtime_ns"], record["mtime_ns"]))
        for name, record in records.items():
            if record["kind"] == "symlink":
                locations[name].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                locations[name].symlink_to(record["link"])
        for name, record in sorted(records.items(), key=lambda item: len(PurePosixPath(item[0]).parts), reverse=True):
            if record["kind"] == "directory":
                locations[name].chmod(record["mode"])
                os.utime(locations[name], ns=(record["mtime_ns"], record["mtime_ns"]))
        for source in manifest["sources"]:
            if not source["included"] and source["kind"] == "directory":
                restored_source = Path(result["source_map"][source["path"]]).relative_to(target_root)
                (data_stage / restored_source).mkdir(mode=0o700, parents=True)
        for mount in manifest["mounts"]:
            if "source_id" in mount and not mount["included"] and mount.get("source_kind") == "directory":
                restored_source = Path(result["source_map"][mount["source"]]).relative_to(target_root)
                (data_stage / restored_source).mkdir(mode=0o700, parents=True, exist_ok=True)
        if manifest.get("include_images"):
            image_target = data_stage / "_images/images.tar"
            image_target.parent.mkdir(mode=0o700)
            shutil.copyfile(bundle / "images.tar", image_target)
            image_target.chmod(0o600)
            expected = manifest["image_archive"]
            if _hash_file(image_target) != (expected["sha256"], expected["size"]):
                raise MigrationError("Image archive changed after validation.")
        _empty_target(target_root)
        _empty_target(config_dir)
        os.replace(data_stage, target_root)
        try:
            # Config may live on another filesystem, so stage its rename there.
            with tempfile.TemporaryDirectory(prefix=".amdl-config-", dir=config_dir.parent) as config_temporary:
                published = Path(config_temporary) / "saved"
                shutil.copytree(config_stage, published, symlinks=True)
                os.replace(published, config_dir)
        except BaseException:
            shutil.rmtree(target_root)
            raise
    return result


def restore_bundle(bundle: Path, root: Path, *, dry_run: bool = False, plan_validator=None) -> dict[str, Any]:
    """Installer adapter; source/ and state/install.lock may already exist."""
    root = Path(root).absolute()
    return restore(bundle, root / "data", root / "config/restored", dry_run=dry_run,
                   plan_validator=plan_validator)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    snap = actions.add_parser("snapshot", help="all selected projects must already be stopped")
    snap.add_argument("project_dir", type=Path)
    snap.add_argument("destination", type=Path)
    snap.add_argument("--include-media", action="store_true")
    snap.add_argument("--include-images", action="store_true")
    verify = actions.add_parser("verify")
    verify.add_argument("bundle", type=Path)
    recover = actions.add_parser("restore")
    recover.add_argument("bundle", type=Path)
    recover.add_argument("root", type=Path)
    recover.add_argument("--dry-run", action="store_true")
    inspect = actions.add_parser("inventory", help="export selected-project metadata without service changes")
    inspect.add_argument("--project", action="append")
    inspect.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "snapshot":
            snapshot(args.project_dir, args.destination, args.include_media, args.include_images)
            print("Private offline snapshot completed and verified.")
        elif args.action == "verify":
            validate_bundle(args.bundle)
            print("Bundle paths, coverage ledger and SHA-256 checks passed.")
        elif args.action == "restore":
            result = restore_bundle(args.bundle, args.root, dry_run=args.dry_run)
            print("Bundle validated; restore plan ready." if args.dry_run else "Private data restored; configuration remapping remains with the installer.")
            print(f"Included data sources: {len(result['source_map'])}; no containers were started.")
        else:
            value = inventory(args.project)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _write_json(args.output, value)
            print("Read-only inventory saved privately; review before creating a deployment descriptor.")
    except (MigrationError, OSError, ValueError, KeyError, TypeError):
        parser.exit(1, "Migration operation refused or failed; no unrelated service actions were taken.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
