"""Bounded unattended-operation evidence, measured only after rollout."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .io import atomic_write_json


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _measure(database: Path, started_at: float, now: float) -> dict[str, Any]:
    with closing(sqlite3.connect("file:" + quote(str(database)) + "?mode=ro", uri=True, timeout=2)) as db:
        deadline = time.monotonic() + 2
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        counts = {status: count for status, count in db.execute(
            "SELECT status,count(*) FROM jobs WHERE created_at>=? AND status!='superseded' GROUP BY status", (started_at,))}
        total = sum(counts.values());completed = counts.get("completed", 0)
        attention = sum(counts.get(k, 0) for k in ("needs_attention", "awaiting_mac_consent", "dead_letter"))
        latency = db.execute("SELECT count(*),avg(finished-created_at),min(finished-created_at),max(finished-created_at) "
            "FROM (SELECT j.created_at,(SELECT min(e.created_at) FROM events e WHERE e.job_id=j.id AND e.event='completed') AS finished "
            "FROM jobs j WHERE j.created_at>=? AND j.status='completed') WHERE finished IS NOT NULL", (started_at,)).fetchone()
        recovered = dict(db.execute("SELECT e.event,count(*) FROM jobs j CROSS JOIN events e ON j.id=e.job_id "
            "WHERE j.created_at>=? AND e.created_at>=? AND e.event IN ('source_recovered','dependency_recovery_resumed') GROUP BY e.event",
            (started_at, started_at)))
        manual_condition = "(e.event IN ('mac_alignment_approved','manual_candidate_submitted','manual_review') OR " \
            "(e.event='source_context_changed' AND CASE WHEN json_valid(e.detail_json) THEN json_extract(e.detail_json,'$.manual_review') ELSE 0 END=1))"
        manual_jobs = db.execute("SELECT count(*) FROM jobs j WHERE j.created_at>=? AND j.status!='superseded' "
            "AND EXISTS(SELECT 1 FROM events e WHERE e.job_id=j.id AND " + manual_condition + ")", (started_at,)).fetchone()[0]
        automatic = db.execute("SELECT count(*) FROM jobs j WHERE j.created_at>=? AND j.status='completed' "
            "AND NOT EXISTS(SELECT 1 FROM events e WHERE e.job_id=j.id AND " + manual_condition + ")", (started_at,)).fetchone()[0]
        historical_count = db.execute("SELECT count(*) FROM jobs WHERE created_at<? AND status='dead_letter'", (started_at,)).fetchone()[0]
        historical_ids = [row[0] for row in db.execute("SELECT id FROM jobs WHERE created_at<? AND status='dead_letter' ORDER BY id LIMIT 500", (started_at,))]
    return {"measurement_status": "active", "measured_at": now, "rollout_started_at": started_at,
        "denominator": "non_superseded_jobs_created_since_rollout", "total_new_jobs": total,
        "completed": completed, "pending": total - completed - attention, "needs_attention": attention,
        "completion_rate": completed / total if total else None,
        "attention_rate": attention / total if total else None,
        "manual_event_jobs": manual_jobs, "auto_completed_without_manual_event": automatic,
        "auto_completion_rate": automatic / total if total else None,
        "historical_dead_letter_count": historical_count, "historical_dead_letter_ids": historical_ids,
        "historical_dead_letter_ids_truncated": historical_count > len(historical_ids),
        "source_recoveries": recovered.get("source_recovered", 0),
        "verified_dependency_resumptions": recovered.get("dependency_recovery_resumed", 0),
        "completion_latency_seconds": {"count": latency[0], "mean": latency[1], "minimum": latency[2], "maximum": latency[3]}}


def update_telemetry(automation_dir: Path, health: dict[str, Any], *, rollout_started_at: float | None,
                     now: float, timezone_name: str = "UTC", interval_seconds: float = 60,
                     daily_retention: int = 90, change_retention: int = 256) -> dict[str, Any]:
    path = automation_dir / "service-telemetry.json";state = _load(path)
    if state.get("schema_version") != 1 or state.get("rollout_started_at") != rollout_started_at:
        state = {"schema_version": 1, "rollout_started_at": rollout_started_at, "daily": {}, "state_changes": []}
    metrics = state.get("metrics") or {};due = not metrics or now - float(metrics.get("measured_at") or 0) >= max(30, interval_seconds)
    if due:
        if not rollout_started_at or rollout_started_at > now:
            metrics = {"measurement_status": "not_configured", "measured_at": now,
                "rollout_started_at": rollout_started_at, "total_new_jobs": 0, "completion_rate": None,
                "attention_rate": None, "manual_event_jobs": 0, "auto_completed_without_manual_event": 0,
                "auto_completion_rate": None, "explanation": "No rollout measurement window configured; historical jobs are excluded."}
        else:
            try:
                metrics = _measure(automation_dir / "queue.sqlite3", rollout_started_at, now)
            except sqlite3.Error:
                metrics = {"measurement_status": "unavailable", "measured_at": now,
                    "rollout_started_at": rollout_started_at, "completion_rate": None, "attention_rate": None,
                    "auto_completion_rate": None}
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            zone = timezone.utc
        day = datetime.fromtimestamp(now, zone).date().isoformat()
        daily = state.setdefault("daily", {});daily[day] = {**metrics, "business_status": health.get("status")}
        state["daily"] = {key: daily[key] for key in sorted(daily)[-max(1, daily_retention):]}
        state["metrics"] = metrics
    summary = {"status": health.get("status"), "reason_codes": sorted(str(x.get("code")) for x in health.get("reasons") or []),
        "attention_count": len(health.get("needs_attention") or [])}
    changed = state.get("last_state") != summary
    if changed:
        changes = list(state.get("state_changes") or []);changes.append({"at": now, **summary})
        state["state_changes"] = changes[-max(1, change_retention):];state["last_state"] = summary
    if due or changed:
        atomic_write_json(path, state);path.chmod(0o600)
    return {**metrics, "evidence_path": str(path), "retained_days": len(state.get("daily") or {}),
        "retained_state_changes": len(state.get("state_changes") or [])}
