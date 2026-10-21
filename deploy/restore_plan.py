"""Explicit service mapping for private legacy bundles, before any restore writes."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Iterable

SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
OPTIONAL_MEDIA = {"navidrome", "emby", "lrcapi"}


class RestorePlanError(RuntimeError):
    pass


def _mapping(value) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RestorePlanError("restore_services must be an explicit source-to-service object.")
    result = {}
    for source, destination in value.items():
        if not isinstance(source, str) or not isinstance(destination, str):
            raise RestorePlanError("Service mappings must contain string names.")
        parts = source.split("/")
        if len(parts) not in {1, 2} or any(not SERVICE_NAME.fullmatch(part) for part in parts):
            raise RestorePlanError("Unsafe source service mapping name.")
        if not SERVICE_NAME.fullmatch(destination):
            raise RestorePlanError("Unsafe destination service mapping name.")
        result[source] = destination
    return result


def load_service_mapping(config_path: Path | None) -> dict[str, str]:
    """Read only top-level restore_services; env values are never returned/logged."""
    if config_path is None:
        return {}
    path = Path(config_path)
    if path.is_symlink() or not path.is_file():
        raise RestorePlanError("The private migration configuration must be an ordinary file.")
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RestorePlanError("Invalid private migration configuration.") from None
    if not isinstance(content, dict):
        raise RestorePlanError("The private migration configuration must be an object.")
    return _mapping(content.get("restore_services", {}))


def normalize_restore_plan(plan: dict, service_map: dict[str, str] | None = None,
                           *, allowed_services: Iterable[str]) -> dict:
    """Keep every mount/image, or refuse the plan; never silently drop a service."""
    if not isinstance(plan, dict):
        raise RestorePlanError("Invalid private restore plan.")
    mapping = _mapping(service_map)
    allowed = set(allowed_services)
    if not allowed or any(not isinstance(name, str) or not SERVICE_NAME.fullmatch(name) for name in allowed):
        raise RestorePlanError("An explicit public service allowlist is required.")
    for destination in mapping.values():
        if destination not in allowed:
            if destination in OPTIONAL_MEDIA:
                raise RestorePlanError("This restore includes media services; explicitly select --with-media-servers.")
            raise RestorePlanError("A service mapping targets a service outside this installation.")
    normalized = deepcopy(plan)
    project_ids, project_names = {}, {}
    for project in normalized.get("projects", []):
        if not isinstance(project, dict):
            raise RestorePlanError("Invalid project metadata in the restore plan.")
        identifier, name = project.get("id"), project.get("project_name")
        if identifier:
            project_ids[identifier] = project
        if name:
            project_names[name] = project
    consumed = set()

    def translate(record):
        if not isinstance(record, dict):
            raise RestorePlanError("Invalid mount/image restore record.")
        old = record.get("original_service", record.get("service"))
        if not isinstance(old, str) or not SERVICE_NAME.fullmatch(old):
            raise RestorePlanError("Invalid source service in the restore plan.")
        project = project_ids.get(record.get("original_project_id", record.get("project_id")))
        name = record.get("original_project", record.get("project"))
        if project is None and name:
            project = project_names.get(name) or project_ids.get(name)
        identifiers = {value for value in (name, (project or {}).get("id"), (project or {}).get("project_name")) if value}
        keys = [key for key in mapping if key == old or any(key == qualifier + "/" + old for qualifier in identifiers)]
        targets = {mapping[key] for key in keys}
        if len(targets) > 1:
            raise RestorePlanError("Ambiguous qualified/unqualified service mappings require review.")
        target = next(iter(targets)) if targets else old
        consumed.update(keys)
        if target not in allowed:
            if target in OPTIONAL_MEDIA:
                raise RestorePlanError("This bundle includes media services; explicitly select --with-media-servers.")
            raise RestorePlanError("A restored service requires an explicit restore_services mapping.")
        record["original_service"] = old
        record["original_project"] = (project or {}).get("project_name") or name or ""
        if project and project.get("id"):
            record["original_project_id"] = project["id"]
        record["service"] = target
        return record

    originals = normalized.get("service_mapping_sources", {})
    if not isinstance(originals, dict):
        raise RestorePlanError("Invalid original service-mapping records.")
    raw_mounts = deepcopy(originals.get("mounts", normalized.get("mounts", [])))
    raw_images = deepcopy(originals.get("images", normalized.get("images", [])))
    normalized["service_mapping_sources"] = {"mounts": deepcopy(raw_mounts), "images": deepcopy(raw_images)}
    mounts, mount_targets = [], {}
    if not isinstance(raw_mounts, list):
        raise RestorePlanError("Invalid mount restore list.")
    for original in raw_mounts:
        record = translate(original)
        target = record.get("target")
        if not isinstance(target, str) or not target.startswith("/") or ".." in Path(target).parts:
            raise RestorePlanError("Unsafe restored container mount target.")
        key = (record["service"], target)
        previous = mount_targets.get(key)
        if previous is not None:
            identity = lambda item: (item.get("restored_path") or item.get("source"),
                                     item.get("source_relative", "."), item.get("included"), bool(item.get("read_only")))
            if identity(previous) != identity(record):
                raise RestorePlanError("Different old data sources map to the same service mount target.")
            previous.setdefault("merged_origins", []).append({"service": record["original_service"],
                                                             "project": record["original_project"]})
            continue
        mount_targets[key] = record
        mounts.append(record)
    images, image_targets = [], {}
    if not isinstance(raw_images, list):
        raise RestorePlanError("Invalid image restore list.")
    for original in raw_images:
        record = translate(original)
        image = record.get("image_id")
        if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise RestorePlanError("Invalid restored image identity.")
        name = record["service"]
        previous = image_targets.get(name)
        if previous is not None:
            if previous["image_id"] != image:
                raise RestorePlanError("Different old service images map to one public service.")
            previous.setdefault("merged_origins", []).append({"service": record["original_service"],
                                                             "project": record["original_project"]})
            continue
        image_targets[name] = record
        images.append(record)
    if consumed != mapping.keys():
        raise RestorePlanError("An explicit service mapping matches no source record; review the migration plan.")
    normalized["mounts"], normalized["images"] = mounts, images
    normalized["service_mapping"] = dict(mapping)
    return normalized
