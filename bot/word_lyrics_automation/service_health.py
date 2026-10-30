"""Read-only business health, bounded canaries and persistent probe backoff.

This module never changes jobs, media, routing, notifications or containers.
Business degradation is deliberately separate from a restart recommendation.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Mapping

from .io import atomic_write_json


SCHEMA_VERSION = 1
POLICY_VERSION = "unattended-health-v1"
TERMINAL = {"completed", "dead_letter", "superseded"}
PROGRESS_EVENTS = ("claimed", "needs_alignment", "candidate_submitted", "sidecar_published", "completed")
STAGE_LIMITS = {"source": 600, "alignment": 7200, "mac_alignment": 7200, "validation": 300, "scan": 600}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, value: Any) -> None:
    atomic_write_json(path, value)
    path.chmod(0o600)


def _safe_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _stage(status: str) -> str:
    for stage in ("mac_alignment", "alignment", "validation", "source", "scan"):
        if stage in status or (stage == "validation" and status == "candidate_pending"):
            return stage
    return "unknown"


def queue_snapshot(database_path: Path, *, now: float | None = None) -> dict[str, Any]:
    """Read bounded active details; neither opening nor querying mutates a queue."""
    now = time.time() if now is None else now
    if not database_path.is_file():
        return {"quick_check": "missing", "counts": {}, "jobs": [], "error": "queue_missing"}
    try:
        with closing(sqlite3.connect("file:" + urllib.parse.quote(str(database_path)) + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=2000")
            quick = db.execute("PRAGMA quick_check").fetchone()[0]
            counts = {str(r[0]): int(r[1]) for r in db.execute("SELECT status,count(*) FROM jobs GROUP BY status")}
            rows = db.execute("SELECT id,status,lease_expires_at,created_at,updated_at,payload_json,last_error "
                "FROM jobs WHERE status NOT IN ('completed','dead_letter','superseded') "
                "ORDER BY created_at,id LIMIT 501").fetchall()
            jobs = []
            for row in rows[:500]:
                payload = _safe_json(row["payload_json"])
                state = payload.get("source_state") or {}
                if not isinstance(state, dict):
                    state = {}
                progress = payload.get("nas_alignment_progress") or {}
                if not isinstance(progress, dict):
                    progress = {}
                recent = db.execute("SELECT created_at FROM events WHERE job_id=? "
                    "AND event IN ('claimed','needs_alignment','candidate_submitted','sidecar_published','completed') "
                    "ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
                jobs.append({"id": int(row["id"]), "status": str(row["status"]),
                    "created_at": float(row["created_at"]), "updated_at": float(row["updated_at"]),
                    "lease_expires_at": float(row["lease_expires_at"] or 0),
                    "stage_started_at": float(recent[0]) if recent else float(row["created_at"]),
                    "fault": {k: state.get(k) for k in ("category", "code", "retryable", "budget_exhausted", "first_failure_at", "attempts") if k in state},
                    "error_fingerprint": hashlib.sha256(str(row["last_error"] or "").encode()).hexdigest() if row["last_error"] else "",
                    "progress": {k: progress.get(k) for k in ("stage", "unit", "completed_units") if k in progress}})
            return {"quick_check": quick, "counts": counts, "active": sum(v for k, v in counts.items() if k not in TERMINAL),
                "details_truncated": len(rows) > 500, "jobs": jobs, "checked_at": now}
    except sqlite3.Error:
        # No database exception text or record contents enter health logs.
        return {"quick_check": "unavailable", "counts": {}, "jobs": [], "error": "queue_read_failed"}


def evaluate_health(*, worker: Mapping[str, Any], queue: Mapping[str, Any], probes: Mapping[str, Any],
                    now: float, worker_max_age: float = 300, probe_max_age: float = 3600,
                    previous_observations: Mapping[str, Any] | None = None,
                    stage_limits: Mapping[str, float] | None = None) -> dict[str, Any]:
    """Pure assessment. Fresh heartbeats cannot hide failed business probes."""
    reasons: list[dict[str, Any]] = []
    observations: dict[str, Any] = {}
    previous_observations = previous_observations or {}
    limits = {**STAGE_LIMITS, **(stage_limits or {})}
    restart_recommended = False
    if queue.get("quick_check") != "ok":
        reasons.append({"code": "queue_unavailable", "severity": "unhealthy", "action": "inspect_storage"})
    updated = float(worker.get("updated_at") or 0)
    worker_age = max(0, now - updated) if updated else None
    if worker_age is None:
        reasons.append({"code": "worker_health_missing", "severity": "degraded", "action": "check_worker"})
    elif worker_age > worker_max_age:
        reasons.append({"code": "worker_heartbeat_stale", "severity": "unhealthy", "action": "restart_worker"})
        restart_recommended = True
    for name in dict.fromkeys(["source", "player", *probes]):
        record = probes.get(name) or {}
        checked_at = float(record.get("checked_at") or 0)
        if not record.get("configured", True):
            reasons.append({"code": name + "_probe_not_configured", "severity": "degraded", "action": "configure_probe"})
        elif not checked_at or now - checked_at > probe_max_age:
            reasons.append({"code": name + "_probe_stale", "severity": "degraded", "action": "run_probe"})
        elif not record.get("ok"):
            reasons.append({"code": name + "_probe_failed", "severity": "degraded", "action": "check_dependency", "fault_code": str(record.get("code") or "probe_failed")[:80]})
    attention = []
    for job in queue.get("jobs") or []:
        jid = str(job["id"]);status = str(job.get("status") or "")
        fault = job.get("fault") or {}
        signature_data = [status, job.get("error_fingerprint"), job.get("progress") or {}]
        signature = hashlib.sha256(json.dumps(signature_data, sort_keys=True).encode()).hexdigest()
        previous = previous_observations.get(jid) or {}
        changed_at = float(previous.get("changed_at") or job.get("stage_started_at") or now)
        if previous and previous.get("signature") != signature:
            changed_at = now
        observations[jid] = {"signature": signature, "changed_at": changed_at, "status": status}
        if status == "needs_attention":
            attention.append({"job_id": job["id"], "code": fault.get("code", "input_or_policy_review_required"), "category": fault.get("category", "unknown")})
            continue
        expires = float(job.get("lease_expires_at") or 0)
        if status.startswith("leased_") and expires and now - expires > 120:
            reasons.append({"code": "lease_not_reclaimed", "severity": "unhealthy", "action": "check_queue_recovery", "job_id": job["id"]})
        stage = _stage(status)
        if status.startswith("leased_") and now - changed_at > float(limits.get(stage, 3600)):
            reasons.append({"code": "job_progress_stalled", "severity": "degraded", "action": "check_stage_deadline", "job_id": job["id"], "stage": stage})
        # A retry loop can update heartbeats/claim timestamps indefinitely.
        # Its original structured failure time remains an independent signal.
        since = float(fault.get("first_failure_at") or 0)
        if since and fault.get("retryable") and now - since > 3600:
            reasons.append({"code": "dependency_retries_not_resolving", "severity": "degraded", "action": "check_dependency", "job_id": job["id"], "fault_code": fault.get("code", "unknown")})
    if attention:
        reasons.append({"code": "jobs_need_attention", "severity": "needs_attention", "action": "inspect_inputs", "count": len(attention)})
    severities = {r["severity"] for r in reasons}
    status = next((value for value in ("unhealthy", "degraded", "needs_attention") if value in severities), "ok")
    return {"schema_version": SCHEMA_VERSION, "policy_version": POLICY_VERSION, "checked_at": now,
        "status": status, "business_ok": status == "ok", "liveness_ok": "unhealthy" not in severities,
        "restart_recommended": restart_recommended, "reasons": reasons, "needs_attention": attention,
        "worker_age_seconds": worker_age, "queue_counts": dict(queue.get("counts") or {}),
        "active_jobs": queue.get("active", len(queue.get("jobs") or [])),
        "probe_results": dict(probes), "job_observations": observations}


def _probe_failure(exc: Exception) -> dict[str, Any]:
    if hasattr(exc, "code") and type(getattr(exc, "retryable", None)) is bool:
        code = str(exc.code)
        if re.fullmatch(r"[a-z0-9_]{1,100}", code):
            return {"ok": False, "code": code, "retryable": exc.retryable}
    if isinstance(exc, urllib.error.HTTPError):
        status = int(exc.code)
        exc.close()
        code = "authentication_or_permission" if status in (401, 403) else "rate_limited" if status == 429 else "http_server_error" if status >= 500 else "http_rejected"
        return {"ok": False, "code": code, "http_status": status, "retryable": status == 429 or status >= 500}
    if isinstance(exc, (ValueError, TypeError, KeyError, zlib.error)):
        return {"ok": False, "code": "invalid_provider_response", "retryable": False}
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return {"ok": False, "code": "probe_deadline_exceeded", "retryable": True}
    if isinstance(exc, (OSError, urllib.error.URLError)):
        return {"ok": False, "code": "transport_failure", "retryable": True}
    return {"ok": False, "code": "probe_internal_error", "retryable": False, "error_type": type(exc).__name__}


def _request_json(opener: Any, url: str, *, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "WordLyricsHealth/1", "Referer": "https://www.kugou.com/"})
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("oversize response")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("non-object response")
    return result


def kugou_canary(spec: Mapping[str, Any], *, opener: Any = None, timeout: float = 5) -> dict[str, Any]:
    """Search, fetch and decode an explicit official public canary recording."""
    if not all(spec.get(k) for k in ("title", "artist", "duration_ms", "lyric_id")):
        return {"ok": False, "configured": False, "code": "canary_recording_not_configured", "retryable": False}
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        query = urllib.parse.urlencode({"ver": 1, "man": "yes", "client": "pc", "keyword": str(spec["artist"]) + " - " + str(spec["title"]), "duration": int(spec["duration_ms"]), "hash": ""})
        result = _request_json(opener, "https://lyrics.kugou.com/search?" + query, timeout=timeout)
        candidates = result.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError("candidates missing")
        item = next((x for x in candidates if isinstance(x, dict) and str(x.get("id")) == str(spec["lyric_id"])), None)
        if item is None:
            return {"ok": False, "code": "canary_recording_unavailable", "retryable": True}
        from .identity import artist_matches, title_matches
        if ("官方" not in str(item.get("product_from") or "")
                or not title_matches(str(spec["title"]), str(item.get("song") or ""))
                or not artist_matches(str(spec["artist"]), str(item.get("singer") or ""))
                or abs(int(spec["duration_ms"]) - int(item.get("duration") or 0)) > 750):
            return {"ok": False, "code": "canary_identity_changed", "retryable": False}
        query = urllib.parse.urlencode({"ver": 1, "client": "pc", "id": item["id"], "accesskey": item["accesskey"], "fmt": "krc", "charset": "utf8"})
        result = _request_json(opener, "https://lyrics.kugou.com/download?" + query, timeout=timeout)
        encrypted = base64.b64decode(str(result["content"]), validate=True)
        if not encrypted.startswith(b"krc1") or len(encrypted) > 512 * 1024:
            raise ValueError("invalid KRC")
        from .providers.kugou import _KRC_KEY, parse_krc
        compressed = bytes(value ^ _KRC_KEY[i % len(_KRC_KEY)] for i, value in enumerate(encrypted[4:]))
        decoder = zlib.decompressobj();plain = decoder.decompress(compressed, 1024 * 1024 + 1)
        if len(plain) > 1024 * 1024 or not decoder.eof:
            raise ValueError("oversize or incomplete KRC")
        lines = parse_krc(plain.decode("utf-8", "strict"), title=str(spec["title"]), artist=str(spec["artist"]))
        cues = sum(len(line.tokens) for line in lines)
        if len(lines) < int(spec.get("minimum_lines", 4)) or cues < int(spec.get("minimum_cues", 20)):
            return {"ok": False, "code": "canary_lyric_semantics_invalid", "retryable": False}
        return {"ok": True, "code": "official_search_download_decoded", "provider": "kugou", "lines": len(lines), "cues": cues,
            "krc_sha256": hashlib.sha256(plain).hexdigest(), "transport": "https_no_environment_proxy"}
    except Exception as exc:
        return _probe_failure(exc)


def amll_canary(spec: Mapping[str, Any], *, opener: Any = None, timeout: float = 5) -> dict[str, Any]:
    """An empty, well-formed discovery response proves API availability only."""
    if not spec.get("title"):
        return {"ok": False, "configured": False, "code": "amll_query_not_configured", "retryable": False}
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        query = urllib.parse.urlencode({"musicName": str(spec["title"]), "artistName": str(spec.get("artist") or ""), "page": 1, "pageSize": 1})
        payload = _request_json(opener, "https://api.amll.dev/v1/lyrics/search?" + query, timeout=timeout)
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("invalid AMLL discovery response")
        return {"ok": True, "code": "valid_source_discovery_response", "provider": "amll",
            "result_count": len(data["items"]), "scope": "discovery_api_availability_not_recording_match",
            "transport": "https_no_environment_proxy"}
    except Exception as exc:
        return _probe_failure(exc)


def netease_canary(spec: Mapping[str, Any], *, opener: Any = None, timeout: float = 5) -> dict[str, Any]:
    """Read a known public lyric record and parse actual word timing."""
    if not spec.get("song_id"):
        return {"ok": False, "configured": False, "code": "netease_record_not_configured", "retryable": False}
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        query = urllib.parse.urlencode({"id": str(spec["song_id"]), "lv": -1, "kv": -1, "tv": -1, "yv": -1, "rv": -1})
        payload = _request_json(opener, "https://music.163.com/api/song/lyric?" + query, timeout=timeout)
        if payload.get("code") not in (None, 200):
            return {"ok": False, "code": "provider_response_rejected", "retryable": False}
        line_data, word_data = payload.get("lrc") or {}, payload.get("yrc") or {}
        if not isinstance(line_data, dict) or not isinstance(word_data, dict):
            raise ValueError("invalid NetEase lyric record")
        lrc, yrc = line_data.get("lyric"), word_data.get("lyric")
        if not isinstance(lrc, str) or not lrc.strip() or not isinstance(yrc, str) or not yrc.strip():
            return {"ok": False, "code": "canary_lyric_unavailable", "retryable": False}
        from .providers.netease import parse_yrc
        lines = parse_yrc(yrc);cues = sum(len(line.tokens) for line in lines)
        if len(lines) < int(spec.get("minimum_lines", 4)) or cues < int(spec.get("minimum_cues", 20)):
            return {"ok": False, "code": "canary_lyric_semantics_invalid", "retryable": False}
        return {"ok": True, "code": "known_record_word_lyrics_decoded", "provider": "netease",
            "lines": len(lines), "cues": cues, "yrc_sha256": hashlib.sha256(yrc.encode()).hexdigest(),
            "transport": "https_no_environment_proxy"}
    except Exception as exc:
        return _probe_failure(exc)


def navidrome_canary(spec: Mapping[str, Any], *, opener: Any = None, timeout: float = 5) -> dict[str, Any]:
    """Read exact rendered word cues for one known published sidecar."""
    user = os.environ.get("NAVIDROME_USER", "");password = os.environ.get("NAVIDROME_PASSWORD", "")
    if not user or not password or not spec.get("song_id") or not spec.get("sidecar_path"):
        return {"ok": False, "configured": False, "code": "player_canary_not_configured", "retryable": False}
    base = os.environ.get("NAVIDROME_URL", "http://navidrome:4533").rstrip("/")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        expected_media = spec.get("source_fingerprint") or spec.get("media_fingerprint")
        media = Path(str(spec["media_path"])) if spec.get("media_path") else None
        if expected_media and media is None:
            return {"ok": False, "configured": False, "code": "player_media_binding_not_configured", "retryable": False}
        if media is not None and expected_media:
            from .fingerprint import media_fingerprint
            if media_fingerprint(media) != expected_media:
                return {"ok": False, "code": "canary_media_changed", "retryable": False}
        sidecar = Path(str(spec["sidecar_path"]))
        if not sidecar.is_file() or sidecar.stat().st_size > 4 * 1024 * 1024:
            return {"ok": False, "code": "canary_sidecar_missing", "retryable": False}
        raw = sidecar.read_bytes();digest = hashlib.sha256(raw).hexdigest()
        if spec.get("candidate_sha256") and digest != spec["candidate_sha256"]:
            return {"ok": False, "code": "canary_sidecar_changed", "retryable": False}
        salt = secrets.token_hex(8);token = hashlib.md5((password + salt).encode(), usedforsecurity=False).hexdigest()
        query = urllib.parse.urlencode({"u": user, "t": token, "s": salt, "v": "1.16.1", "c": "word-lyrics-health", "f": "json", "id": str(spec["song_id"]), "enhanced": "true"})
        response = _request_json(opener, base + "/rest/getLyricsBySongId.view?" + query, timeout=timeout)["subsonic-response"]
        if response.get("status") != "ok":
            code = (response.get("error") or {}).get("code")
            return {"ok": False, "code": "player_authentication_rejected" if code in (40, 41, 50) else "player_api_rejected", "retryable": False}
        actual = [x for x in response.get("lyricsList", {}).get("structuredLyrics", []) if x.get("kind", "main") == "main"]
        from .player_verification import compare_word_lyrics
        metrics = compare_word_lyrics(raw.decode("utf-8-sig"), actual)
        if media is not None and expected_media and media_fingerprint(media) != expected_media:
            return {"ok": False, "code": "canary_media_changed", "retryable": False}
        if hashlib.sha256(sidecar.read_bytes()).hexdigest() != digest:
            return {"ok": False, "code": "canary_sidecar_changed", "retryable": False}
        return {"ok": True, "code": "exact_published_word_cues_read_back", **metrics,
            "sidecar_sha256": digest, "media_binding_verified": bool(media and expected_media)}
    except Exception as exc:
        return _probe_failure(exc)


def bounded_probe(kind: str, spec: Mapping[str, Any], *, deadline_seconds: float = 20) -> dict[str, Any]:
    """A killed subprocess provides a real wall-clock bound even on stuck DNS."""
    try:
        child = subprocess.run([sys.executable, "-m", "word_lyrics_automation.service_health", "probe", kind], input=json.dumps(dict(spec)),
            capture_output=True, text=True, timeout=max(1, deadline_seconds), check=False)
        if child.returncode or len(child.stdout) > 65536:
            return {"ok": False, "code": "probe_process_failed", "retryable": True}
        result = json.loads(child.stdout)
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError("invalid probe result")
        return result
    except Exception as exc:
        return _probe_failure(exc)


class ServiceHealthMonitor:
    def __init__(self, automation_dir: Path, *, clock: Callable[[], float] = time.time,
                 source_spec: Mapping[str, Any] | None = None, player_spec: Mapping[str, Any] | None = None,
                 source_probe: Callable[[], dict[str, Any]] | None = None, player_probe: Callable[[], dict[str, Any]] | None = None,
                 additional_source_specs: Mapping[str, Mapping[str, Any]] | None = None,
                 additional_source_probes: Mapping[str, Callable[[], dict[str, Any]]] | None = None,
                 probe_interval: float = 900, probe_max_age: float = 3600, worker_max_age: float = 300,
                 probe_deadline_seconds: float = 20, rollout_started_at: float | None = None,
                 timezone_name: str = "UTC") -> None:
        self.automation_dir = Path(automation_dir);self.automation_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock;self.probe_interval = max(30, probe_interval);self.probe_max_age = max(self.probe_interval, probe_max_age)
        self.worker_max_age = max(30, worker_max_age);self.probe_deadline_seconds = max(1, probe_deadline_seconds)
        self.rollout_started_at = rollout_started_at;self.timezone_name = timezone_name
        self.report_path = self.automation_dir / "service-health.json"
        self.state_path = self.automation_dir / "service-probe-state.json"
        self.specs = {"source": dict(source_spec or {}), "player": dict(player_spec or {})}
        self.providers = {"source": "kugou", "player": "navidrome"}
        for provider, spec in (additional_source_specs or {}).items():
            if provider not in {"amll", "netease"}:
                raise ValueError("unsupported source health provider")
            self.specs["source_" + provider] = dict(spec)
            self.providers["source_" + provider] = provider
        self.probes = {"source": source_probe or (lambda: bounded_probe("source", self.specs["source"], deadline_seconds=self.probe_deadline_seconds)),
            "player": player_probe or (lambda: bounded_probe("player", self.specs["player"], deadline_seconds=self.probe_deadline_seconds))}
        for provider in (additional_source_specs or {}):
            key = "source_" + provider
            self.probes[key] = (additional_source_probes or {}).get(provider) or (
                lambda provider=provider, key=key: bounded_probe(provider, self.specs[key], deadline_seconds=self.probe_deadline_seconds))

    @classmethod
    def from_environment(cls, automation_dir: Path) -> "ServiceHealthMonitor":
        config = _read_json(Path(os.environ.get("WORD_LYRICS_HEALTH_CONFIG", str(automation_dir / "service-health-config.json"))))
        return cls(automation_dir, source_spec=config.get("source") or {}, player_spec=config.get("player") or {},
            additional_source_specs=config.get("additional_sources") or {},
            rollout_started_at=config.get("rollout_started_at"), timezone_name=str(config.get("timezone") or "UTC"),
            probe_interval=float(os.environ.get("WORD_LYRICS_PROBE_INTERVAL_SECONDS", "900")),
            probe_max_age=float(os.environ.get("WORD_LYRICS_PROBE_MAX_AGE_SECONDS", "3600")),
            worker_max_age=float(os.environ.get("WORD_LYRICS_WORKER_MAX_AGE_SECONDS", "300")))

    def update(self, worker_health: Mapping[str, Any] | None = None, *, run_probes: bool = True) -> dict[str, Any]:
        now = self.clock()
        with (self.automation_dir / "service-health.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                cached = _read_json(self.report_path)
                return cached or {"status": "degraded", "business_ok": False, "liveness_ok": True,
                    "checked_at": now, "restart_recommended": False, "reasons": [{"code": "health_probe_in_progress", "severity": "degraded"}]}
            state = _read_json(self.state_path)
            if state.get("policy_version") != POLICY_VERSION:
                state = {"schema_version": SCHEMA_VERSION, "policy_version": POLICY_VERSION, "probes": {}}
            records = {key: value for key, value in (state.get("probes") or {}).items() if key in self.specs}
            state["probes"] = records
            for name in self.specs:
                key = hashlib.sha256(json.dumps(self.specs[name], sort_keys=True).encode()).hexdigest()
                record = records.get(name) or {}
                if record.get("context_key") != key:
                    record = {}
                if run_probes and (not record or now >= float(record.get("next_probe_at") or 0)):
                    started = time.monotonic()
                    try:
                        result = self.probes[name]()
                        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                            raise ValueError("invalid probe result")
                    except Exception as exc:
                        result = _probe_failure(exc)
                    checked_at = self.clock();failures = 0 if result["ok"] else int(record.get("consecutive_failures") or 0) + 1
                    delay = min(self.probe_interval * (2 ** min(failures, 3)), 7200) if failures else self.probe_interval
                    transitioned = (result["ok"] and record.get("ok") is False
                        and record.get("configured", True) is not False and bool(record.get("checked_at")))
                    generation = int(record.get("recovery_generation") or 0) + int(transitioned)
                    last_failure = record.get("last_failure_at")
                    if not result["ok"] and result.get("configured", True):
                        last_failure = checked_at
                    provider = self.providers[name]
                    record = {**result, "checked_at": checked_at, "context_key": key, "consecutive_failures": failures,
                        "last_success_at": checked_at if result["ok"] else record.get("last_success_at"),
                        "name": provider, "provider": provider, "capability": "player" if name == "player" else "source",
                        "status": "ok" if result["ok"] else "failed" if result.get("configured", True) else "not_configured",
                        "last_failure_at": last_failure,
                        "recovery_generation": generation,
                        "recovered_at": checked_at if transitioned else record.get("recovered_at"),
                        "next_probe_at": checked_at + delay, "elapsed_seconds": round(time.monotonic() - started, 3)}
                records[name] = record
            previous = _read_json(self.report_path)
            current = self.clock()
            worker = dict(worker_health) if worker_health is not None else _read_json(self.automation_dir / "source-worker-health.json")
            snapshot = queue_snapshot(self.automation_dir / "queue.sqlite3", now=current)
            aligner = _read_json(self.automation_dir / "nas-alignment-health.json")
            acoustic = aligner.get("acoustic_progress") or {}
            if isinstance(acoustic, dict):
                for job in snapshot.get("jobs") or []:
                    if job["id"] == aligner.get("job_id"):
                        job["progress"] = {k: acoustic.get(k) for k in ("stage", "unit", "completed_units") if k in acoustic}
            report = evaluate_health(worker=worker, queue=snapshot, probes=records, now=current,
                worker_max_age=self.worker_max_age, probe_max_age=self.probe_max_age,
                previous_observations=previous.get("job_observations") or {})
            report["queue_details_truncated"] = snapshot.get("details_truncated", False)
            from .telemetry import update_telemetry
            report["autonomy_metrics"] = update_telemetry(self.automation_dir, report,
                rollout_started_at=self.rollout_started_at, now=current, timezone_name=self.timezone_name)
            _write_json(self.state_path, state);_write_json(self.report_path, report)
            return report


def main() -> int:
    parser = argparse.ArgumentParser();sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe");probe.add_argument("kind", choices=("source", "player", "amll", "netease"))
    status = sub.add_parser("status");status.add_argument("--automation-dir", type=Path, default=Path(os.environ.get("AMDL_WORD_LYRICS_STATE_DIR", "/state/word-lyrics")) / "automation")
    status.add_argument("--liveness", action="store_true", help="Only worker/queue liveness affects the exit code; still print business degradation")
    status.add_argument("--probe", action="store_true", help="Run due bounded canaries; ordinary status uses cached probes")
    args = parser.parse_args()
    if args.command == "probe":
        try:
            spec = json.loads(sys.stdin.read(65537))
            if not isinstance(spec, dict):
                raise ValueError("invalid probe input")
            function = {"source": kugou_canary, "player": navidrome_canary,
                "amll": amll_canary, "netease": netease_canary}[args.kind]
            result = function(spec)
        except Exception as exc:
            result = _probe_failure(exc)
        print(json.dumps(result, ensure_ascii=False));return 0
    report = ServiceHealthMonitor.from_environment(args.automation_dir).update(run_probes=args.probe)
    # Internal signatures are persisted for progress tracking, not operator UI.
    print(json.dumps({k: v for k, v in report.items() if k != "job_observations"}, ensure_ascii=False))
    successful = report["liveness_ok"] if args.liveness else report["business_ok"]
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
