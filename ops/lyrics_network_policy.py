"""Check or restore the known music/DNS DIRECT constraints, never infer routes.

The default check mode is read-only. Ensure only restores this finite policy;
it is not a response to an arbitrary TLS error and never adds proxy listeners
or fallback groups. Validator/reload/flush callbacks must be bounded by their
caller; the supplied adapters enforce explicit process/HTTP timeouts.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
import stat
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

import yaml


POLICY_VERSION = "music-direct-dns-v1"
MUSIC_SUFFIXES = ("music.apple.com", "itunes.apple.com", "mzstatic.com", "kuwo.cn", "kugou.com",
                  "migu.cn", "music.163.com", "music.126.net", "y.qq.com", "qqmusic.qq.com")
REQUIRED_RULES = ("IP-CIDR,223.5.5.5/32,DIRECT,no-resolve", "IP-CIDR,120.53.53.53/32,DIRECT,no-resolve",
                  *("DOMAIN-SUFFIX," + domain + ",DIRECT" for domain in MUSIC_SUFFIXES),
                  "DOMAIN,y.gtimg.cn,DIRECT", "DOMAIN,api.tencentmusic.com,DIRECT")
LYRICS_HOST = "lyrics.kugou.com"
LYRICS_NAMESERVER = "https://223.5.5.5/dns-query#DIRECT"
_MAX_CONFIG = 8 * 1024 * 1024


class NetworkPolicyError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False, details: dict[str, Any] | None = None):
        super().__init__(code)
        self.code, self.retryable, self.details = code, retryable, details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "retryable": self.retryable, "details": copy.deepcopy(self.details)}


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    explicit = set()
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        if key in explicit:
            raise NetworkPolicyError("policy_config_invalid", details={"reason": "duplicate_yaml_key"})
        explicit.add(key)
    # A deliberate YAML merge override is valid; only duplicate explicit
    # keys are ambiguous. Preserve the SafeLoader's standard merge behavior.
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _document(source: str | dict[str, Any]) -> dict[str, Any]:
    try:
        document = yaml.load(source, Loader=_UniqueLoader) if isinstance(source, str) else copy.deepcopy(source)
        if not isinstance(document, dict) or not isinstance(document.get("rules", []), list):
            raise ValueError("root/rules shape")
        if any(not isinstance(value, str) for value in document.get("rules", [])):
            raise ValueError("rule shape")
        dns = document.get("dns", {})
        if not isinstance(dns, dict) or not isinstance(dns.get("nameserver-policy", {}), dict):
            raise ValueError("dns shape")
        for key in ("listeners", "proxy-groups"):
            if not isinstance(document.get(key, []), list) or any(not isinstance(value, dict) for value in document.get(key, [])):
                raise ValueError("managed safety shape")
        return document
    except NetworkPolicyError:
        raise
    except Exception as exc:
        # YAML parser messages can contain the source line, including secrets.
        raise NetworkPolicyError("policy_config_invalid", details={"exception": type(exc).__name__}) from None


def _is_direct(rule: str) -> bool:
    fields = [field.strip() for field in rule.split(",")]
    return bool(fields and (fields[-1] == "DIRECT" or len(fields) > 1 and fields[-2:] == ["DIRECT", "no-resolve"]))


def check_policy(source: str | dict[str, Any]) -> dict[str, Any]:
    """Return only known-rule diagnostics; never return raw configuration."""
    document = _document(source)
    rules = document.get("rules", [])
    issues = []
    for rule in REQUIRED_RULES:
        if rule not in rules:
            issues.append({"code": "direct_rule_missing", "rule": rule})
        elif any(not _is_direct(prior) for prior in rules[:rules.index(rule)]):
            # Conservatively place known constraints before any policy that
            # could intercept them, not merely before a literal MATCH rule.
            issues.append({"code": "direct_rule_priority", "rule": rule})
    nameservers = document.get("dns", {}).get("nameserver-policy", {}).get(LYRICS_HOST)
    if nameservers not in (LYRICS_NAMESERVER, [LYRICS_NAMESERVER]):
        issues.append({"code": "lyrics_direct_dns_missing", "domain": LYRICS_HOST})
    forbidden_listener = (any(item.get("port") in (7892, "7892") or item.get("name") == "apple-music-recovery"
                              for item in document.get("listeners", []))
                          or any(document.get(key) in (7892, "7892") for key in ("port", "mixed-port", "socks-port")))
    forbidden_group = any(item.get("name") == "AppleMusicNetwork" for item in document.get("proxy-groups", []))
    if forbidden_listener:
        issues.append({"code": "withdrawn_music_listener_present"})
    if forbidden_group:
        issues.append({"code": "withdrawn_music_fallback_present"})
    return {"passed": not issues, "policy_version": POLICY_VERSION, "issues": issues,
            "required_direct_rules": len(REQUIRED_RULES), "no_7892_listener": not forbidden_listener,
            "no_withdrawn_music_fallback": not forbidden_group}


def _unmanaged(document: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(document)
    result["rules"] = [rule for rule in result.get("rules", []) if rule not in REQUIRED_RULES]
    if not result["rules"]:
        result.pop("rules")
    dns = result.get("dns")
    if isinstance(dns, dict):
        policy = dns.get("nameserver-policy")
        if isinstance(policy, dict):
            policy.pop(LYRICS_HOST, None)
            if not policy:
                dns.pop("nameserver-policy")
        if not dns:
            result.pop("dns")
    return result


def render_policy(source: str | dict[str, Any]) -> str:
    """Render a candidate after proving every unowned value is unchanged."""
    original = _document(source)
    checked = check_policy(original)
    if not checked["no_7892_listener"] or not checked["no_withdrawn_music_fallback"]:
        # These objects are not owned by this small repair. Never silently
        # remove or repurpose a user's proxy/listener configuration.
        raise NetworkPolicyError("policy_manual_action_required", details={"reason": "withdrawn_music_recovery_objects"})
    updated = copy.deepcopy(original)
    updated["rules"] = list(REQUIRED_RULES) + [rule for rule in original.get("rules", []) if rule not in REQUIRED_RULES]
    updated.setdefault("dns", {}).setdefault("nameserver-policy", {})[LYRICS_HOST] = [LYRICS_NAMESERVER]
    if _unmanaged(updated) != _unmanaged(original):
        raise NetworkPolicyError("policy_semantic_guard_failed")
    text = yaml.safe_dump(updated, allow_unicode=True, sort_keys=False)
    parsed = _document(text)
    if parsed != updated or not check_policy(parsed)["passed"]:
        raise NetworkPolicyError("policy_semantic_guard_failed")
    return text


def _snapshot(path: Path) -> tuple[bytes, tuple[int, ...], os.stat_result]:
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_CONFIG:
            raise NetworkPolicyError("policy_config_invalid", details={"reason": "file_type_or_size"})
        data = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise NetworkPolicyError("policy_io_error", retryable=True, details={"exception": type(exc).__name__}) from None
    identity = lambda item: (item.st_dev, item.st_ino, item.st_mtime_ns, item.st_ctime_ns,
                             item.st_size, item.st_uid, item.st_gid, item.st_mode)
    if identity(before) != identity(after) or len(data) != after.st_size:
        raise NetworkPolicyError("policy_concurrent_change", retryable=True)
    return data, identity(after), after


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _guard(path: Path, data: bytes, identity: tuple[int, ...]) -> None:
    actual, current_identity, _ = _snapshot(path)
    if actual != data or current_identity != identity:
        raise NetworkPolicyError("policy_concurrent_change", retryable=True)


def _private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".policy-state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _candidate_file(config: Path, data: bytes, info: os.stat_result) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".lyrics-policy-", suffix=".yaml", dir=config.parent)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            # Keep the existing config's owner/mode across atomic replacement.
            current = os.fstat(handle.fileno())
            if (current.st_uid, current.st_gid) != (info.st_uid, info.st_gid):
                os.fchown(handle.fileno(), info.st_uid, info.st_gid)
            os.fchmod(handle.fileno(), stat.S_IMODE(info.st_mode))
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # POSIX ACLs and other config attributes must survive inode replacement.
        # If an attribute cannot be preserved, fail before touching the live file.
        if hasattr(os, "listxattr"):
            for attribute in os.listxattr(config):
                os.setxattr(path, attribute, os.getxattr(config, attribute))
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise NetworkPolicyError("policy_config_invalid", details={"reason": "config_encoding"}) from None


def _callback(callback: Callable[..., Any], code: str, *args) -> None:
    try:
        if callback(*args) is False:
            raise RuntimeError("callback rejected operation")
    except Exception as exc:
        raise NetworkPolicyError(code, retryable=True, details={"exception": type(exc).__name__}) from None


def ensure_policy(config_path: Path | str, state_dir: Path | str | None = None, *, mode: str = "check",
                  validate: Callable[[Path], Any] | None = None,
                  reload_config: Callable[[Path], Any] | None = None,
                  flush_dns: Callable[[], Any] | None = None,
                  cooldown_seconds: float = 300, lock_timeout_seconds: float = 5,
                  clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Check without writes, or validate/backup/apply/reload/flush with rollback.

    A known bad source/candidate pair is attempted at most once per cooldown,
    including failures. A changed source hash is checked as a new candidate.
    State contains only hashes, times and fixed result codes. Config backups
    are private files; neither return values nor exceptions contain secrets.
    """
    if mode not in {"check", "ensure"}:
        raise ValueError("mode must be check or ensure")
    if (not math.isfinite(cooldown_seconds) or not math.isfinite(lock_timeout_seconds)
            or cooldown_seconds < 0 or not 0 <= lock_timeout_seconds <= 60):
        raise ValueError("invalid policy cooldown/lock limit")
    config = Path(config_path).absolute()
    if mode == "check":
        data, _, _ = _snapshot(config)
        checked = check_policy(_text(data))
        return {**checked, "mode": "check", "changed": False, "config_sha256": _sha(data)}
    if not all(callable(value) for value in (validate, reload_config, flush_dns)):
        raise NetworkPolicyError("policy_apply_callbacks_required")
    state = Path(state_dir) if state_dir is not None else config.parent / "maintenance" / "lyrics-network-policy"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state / "policy.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "a") as lock:
        until = time.monotonic() + lock_timeout_seconds
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise NetworkPolicyError("policy_lock_busy", retryable=True)
                time.sleep(min(.05, max(0, until - time.monotonic())))
        candidate: Path | None = None
        original, identity, info = _snapshot(config)
        checked = check_policy(_text(original))
        source_hash = _sha(original)
        if checked["passed"]:
            return {**checked, "mode": "ensure", "changed": False, "action": "unchanged", "config_sha256": source_hash}
        desired_text = render_policy(_text(original))
        desired = desired_text.encode("utf-8")
        desired_hash = _sha(desired)
        status_path = state / "last-attempt.json"
        try:
            previous = json.loads(status_path.read_text())
            age = clock() - float(previous["attempted_at"])
            same = (previous.get("policy_version") == POLICY_VERSION and previous.get("source_sha256") == source_hash
                    and previous.get("candidate_sha256") == desired_hash)
            if same and 0 <= age < cooldown_seconds:
                return {**checked, "mode": "ensure", "changed": False, "action": "cooldown", "config_sha256": source_hash,
                        "retry_after_seconds": round(cooldown_seconds - age, 3)}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        record = {"policy_version": POLICY_VERSION, "source_sha256": source_hash,
                  "candidate_sha256": desired_hash, "attempted_at": clock()}
        applied = False
        applied_identity = None
        backup = state / "backups" / (str(time.time_ns()) + "-" + source_hash[:16] + ".yaml")
        phase = "validation"
        try:
            candidate = _candidate_file(config, desired, info)
            _callback(validate, "policy_validation_failed", candidate)
            # Reparse validated bytes, since validators are external callbacks.
            if candidate.read_bytes() != desired or _document(candidate.read_text()) != _document(desired_text):
                raise NetworkPolicyError("policy_candidate_changed", retryable=True)
            _guard(config, original, identity)
            _private_write(backup, original)
            _guard(config, original, identity)
            os.replace(candidate, config)
            candidate = None
            applied = True
            live, applied_identity, _ = _snapshot(config)
            if live != desired:
                raise NetworkPolicyError("policy_concurrent_change", retryable=True)
            _sync_directory(config.parent)
            phase = "reload"
            _callback(reload_config, "policy_reload_failed", config)
            _guard(config, desired, applied_identity)
            phase = "dns_flush"
            _callback(flush_dns, "policy_dns_flush_failed")
            _guard(config, desired, applied_identity)
            record.update(result="applied", completed_at=clock())
            _private_write(status_path, json.dumps(record, sort_keys=True).encode())
            return {**check_policy(desired_text), "mode": "ensure", "changed": True, "action": "restored",
                    "source_sha256": source_hash, "config_sha256": desired_hash, "backup_path": str(backup),
                    "validated": True, "reloaded": True, "dns_flushed": True}
        except Exception as original_error:
            error = original_error if isinstance(original_error, NetworkPolicyError) else NetworkPolicyError(
                "policy_io_error", retryable=True, details={"exception": type(original_error).__name__})
            rollback = {"file_restored": False, "runtime_restored": False, "dns_flushed": False}
            if applied:
                try:
                    if applied_identity is None:
                        raise NetworkPolicyError("policy_rollback_conflict", retryable=True)
                    _guard(config, desired, applied_identity)
                    restored = _candidate_file(config, original, info)
                    try:
                        os.replace(restored, config)
                        rollback["file_restored"] = True
                        _sync_directory(config.parent)
                    finally:
                        restored.unlink(missing_ok=True)
                    restored_data, restored_identity, _ = _snapshot(config)
                    if restored_data != original:
                        raise NetworkPolicyError("policy_rollback_conflict", retryable=True)
                    _callback(reload_config, "policy_rollback_reload_failed", config)
                    _guard(config, original, restored_identity)
                    rollback["runtime_restored"] = True
                    _callback(flush_dns, "policy_rollback_dns_flush_failed")
                    _guard(config, original, restored_identity)
                    rollback["dns_flushed"] = True
                except Exception as rollback_error:
                    rollback["error"] = rollback_error.code if isinstance(rollback_error, NetworkPolicyError) else type(rollback_error).__name__
            record.update(result="failed", phase=phase, code=error.code, rollback=rollback)
            try:
                _private_write(status_path, json.dumps(record, sort_keys=True).encode())
            except OSError:
                pass
            error.details.update(phase=phase, rollback=rollback)
            raise error from None
        finally:
            if candidate is not None:
                candidate.unlink(missing_ok=True)


def mihomo_validator(binary: Path | str, config_dir: Path | str, timeout_seconds: float = 30) -> Callable[[Path], None]:
    def validate(candidate: Path) -> None:
        process = subprocess.run([str(binary), "-t", "-d", str(config_dir), "-f", str(candidate)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=timeout_seconds)
        if process.returncode:
            raise NetworkPolicyError("policy_validation_failed")
    return validate


def controller_callbacks(base_url: str, runtime_config_path: str, secret: str = "", timeout_seconds: float = 10):
    """Build bounded, explicitly direct controller adapters; never log secrets."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"Authorization": "Bearer " + secret} if secret else {}
    def request(endpoint: str, method: str, body: dict[str, Any] | None = None) -> None:
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(base_url.rstrip("/") + endpoint, data=data, method=method,
                                     headers={**headers, "Content-Type": "application/json"})
        with opener.open(req, timeout=timeout_seconds) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError("controller rejected operation")
    return (lambda _: request("/configs?force=true", "PUT", {"path": runtime_config_path}),
            lambda: request("/cache/dns/flush", "POST"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Check known music DIRECT/DNS constraints; writes require --mode ensure.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--mode", choices=("check", "ensure"), default="check")
    parser.add_argument("--mihomo-bin", default="/mihomo")
    parser.add_argument("--controller", default="http://127.0.0.1:9090")
    parser.add_argument("--runtime-config-path")
    parser.add_argument("--cooldown-seconds", type=float, default=300)
    args = parser.parse_args()
    try:
        callbacks = {}
        if args.mode == "ensure":
            document = _document(args.config.read_text())
            reload_config, flush_dns = controller_callbacks(args.controller, args.runtime_config_path or str(args.config), str(document.get("secret") or ""))
            callbacks = {"validate": mihomo_validator(args.mihomo_bin, args.config.parent), "reload_config": reload_config, "flush_dns": flush_dns}
        result = ensure_policy(args.config, args.state_dir, mode=args.mode, cooldown_seconds=args.cooldown_seconds, **callbacks)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["passed"] else 1
    except Exception as exc:
        error = exc.as_dict() if isinstance(exc, NetworkPolicyError) else {"code": "policy_operation_failed", "details": {"exception": type(exc).__name__}}
        print(json.dumps({"passed": False, "error": error}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
