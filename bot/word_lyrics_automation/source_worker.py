from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import socket
import time
from pathlib import Path
from typing import Any

from .io import atomic_write_json
from .fingerprint import media_fingerprint
from .queue import QueueJob, QueueStore
from .resolver import CandidateRejected, LegacyGateway, SourceResolutionEngine, _rollback_sidecar
from .text import normalize_text


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class SourceWorker:
    def __init__(self) -> None:
        self.state_dir = Path(env("AMDL_WORD_LYRICS_STATE_DIR", "/state/word-lyrics"))
        self.automation_dir = self.state_dir / "automation"
        self.inbox_dir = self.automation_dir / "inbox"
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.store = QueueStore(self.automation_dir / "queue.sqlite3")
        self.gateway = LegacyGateway(self.state_dir)
        self.engine = SourceResolutionEngine(self.gateway)
        self.owner = f"nas-source:{socket.gethostname()}:{os.getpid()}"
        self.poll_seconds = max(1.0, float(env("WORD_LYRICS_SOURCE_POLL_SECONDS", "5")))
        self.lease_seconds = max(
            120.0, float(env("WORD_LYRICS_SOURCE_LEASE_SECONDS", "900"))
        )
        self.health_path = self.automation_dir / "source-worker-health.json"
        self.reconcile_interval = max(
            15.0,
            float(env("WORD_LYRICS_FINGERPRINT_RECONCILE_SECONDS", "60")),
        )
        self.last_reconcile_at = 0.0
        self.running = True

    def health(self, **extra: Any) -> None:
        atomic_write_json(
            self.health_path,
            {
                "updated_at": time.time(),
                "owner": self.owner,
                "pid": os.getpid(),
                "queue": self.store.stats(),
                **extra,
            },
        )

    def character_result(self, media_path: Path, result: dict[str, Any]) -> dict[str, Any]:
        result = dict(result)
        information = self.gateway.service.normalize_character_sidecar(media_path)
        if information:
            result.update({key: information[key] for key in
                ("sidecar_path", "backup_path", "candidate_sha256", "source_candidate_sha256")})
            result["metrics"] = {**result.get("metrics", {}), **information}
        return result

    def process_source(self, job: QueueJob) -> None:
        try:
            resolution = self.engine.resolve(
                media_path=Path(job.media_path),
                metadata=job.metadata,
                expected_fingerprint=job.fingerprint_data,
            )
            if resolution.existing is not None:
                # Reusing a sidecar still needs a scan and final content/audio
                # checks. This job did not publish the existing file, so its
                # original bytes must never be retracted as this job's write.
                result = dict(resolution.existing)
                sidecar = Path(str(result.get("sidecar_path") or ""))
                digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()
                if result.get("candidate_sha256") not in (None, digest):
                    raise RuntimeError("existing lyric changed before scan")
                result["candidate_sha256"] = digest
                result["existing_sidecar_sha256"] = result["candidate_sha256"]
                if media_fingerprint(Path(job.media_path)) != job.fingerprint_data:
                    raise RuntimeError("audio changed before reusing lyric sidecar")
                self.store.transition_to_scan(
                    job.id,
                    job.lease_token,
                    result=result,
                    expected_statuses={"leased_source"},
                )
            elif resolution.publication is not None:
                self.store.transition_to_scan(
                    job.id,
                    job.lease_token,
                    result=resolution.publication.as_dict(),
                    expected_statuses={"leased_source"},
                )
            elif resolution.not_required is not None:
                self.store.transition_to_scan(
                    job.id, job.lease_token, result=resolution.not_required,
                    expected_statuses={"leased_source"},
                )
            else:
                payload = dict(resolution.alignment_payload or {})
                for key in ("source_recovery", "audio_identity", "workflow_created_at", "source_history_from_job"):
                    if key in job.payload:
                        payload[key] = job.payload[key]
                history = list(job.payload.get("source_diagnostics_history") or [])
                history.append({"at": self.store.clock(), "reason": resolution.reason,
                    "errors": payload.get("source_rejections") or [],
                    "observations": payload.get("source_observations") or []})
                payload["source_diagnostics_history"] = history[-24:]
                if not normalize_text(str(payload.get("reference_text") or "")):
                    error = resolution.retryable_error or "no_trusted_lyric_reference"
                    # Initial discovery gets four attempts with real backoff.
                    # Later slow rechecks do one pass each, keeping the job and
                    # its evidence without a repeated terminal-failure notice.
                    waiting = bool((payload.get("source_recovery") or {}).get("wait_cycles"))
                    if not waiting and job.attempts < min(4, job.max_attempts):
                        self.store.fail(job.id, job.lease_token, stage="source", error=error,
                            base_delay_seconds=300, maximum_delay_seconds=3600,
                            diagnostic_payload=payload)
                    else:
                        self.store.wait_for_source(job.id, job.lease_token, error=error,
                            diagnostic_payload=payload)
                    return
                self.store.defer_to_alignment(
                    job.id,
                    job.lease_token,
                    payload=payload,
                    reason=resolution.reason,
                )
        except Exception as exc:
            self.store.fail(
                job.id,
                job.lease_token,
                stage="source",
                error=f"{type(exc).__name__}:{exc}",
            )

    def process_validation(self, job: QueueJob) -> None:
        candidate_path = Path(str(job.payload.get("candidate_path") or ""))
        report = job.payload.get("candidate_report")
        try:
            if not isinstance(report, dict):
                raise CandidateRejected("candidate_report_missing")
            candidate_path.resolve().relative_to(self.inbox_dir.resolve())
            publication = self.engine.validate_mac_candidate(
                media_path=Path(job.media_path),
                metadata=job.metadata,
                expected_fingerprint=job.fingerprint_data,
                candidate_path=candidate_path,
                report=report,
                alignment_payload=job.payload,
            )
            self.store.transition_to_scan(
                job.id,
                job.lease_token,
                result=publication.as_dict(),
                expected_statuses={"leased_validation"},
            )
            try:
                candidate_path.unlink(missing_ok=True)
            except OSError as exc:
                # Publication and the scan handoff are already committed.
                # Retaining a small inbox file must not fail an obsolete
                # validation lease or terminate the worker after success.
                logging.getLogger(__name__).warning(
                    "Lyric candidate cleanup deferred after scan handoff: job=%s error=%s",
                    job.id, type(exc).__name__,
                )
        except CandidateRejected as exc:
            # Retain the small candidate and the precise rejection evidence.
            # Deleting it here used to discard a completed NAS alignment and
            # force another whole-song transcription just to diagnose a bug.
            self.store.fail(
                job.id,
                job.lease_token,
                stage="validation",
                error=str(exc),
                retryable=False,
                diagnostic_payload={
                    **job.payload,
                    "validation_failure": {"reason": str(exc), "metrics": exc.metrics},
                },
            )
        except Exception as exc:
            self.store.fail(
                job.id,
                job.lease_token,
                stage="validation",
                error=f"{type(exc).__name__}:{exc}",
            )

    def process_scan(self, job: QueueJob) -> None:
        result = dict(job.result)

        def verify_publication() -> None:
            if media_fingerprint(Path(job.media_path)) != job.fingerprint_data:
                # Character adaptation may have made a second derived sidecar.
                # Undo it first, then undo the original publication, only while
                # each file is still the exact candidate written by this job.
                for publication in (result, job.result):
                    if (publication.get("existing_sidecar_sha256")
                            == publication.get("candidate_sha256")):
                        continue
                    if publication.get("sidecar_path") and publication.get("candidate_sha256"):
                        _rollback_sidecar(publication["sidecar_path"],
                            publication.get("backup_path", ""), publication["candidate_sha256"])
                raise RuntimeError("audio changed before lyric completion")
            sidecar = Path(str(result.get("sidecar_path") or ""))
            digest = result.get("candidate_sha256")
            if not digest or not sidecar.is_file() or hashlib.sha256(sidecar.read_bytes()).hexdigest() != digest:
                raise RuntimeError("published lyric changed before completion")

        try:
            from youtube_ingest import trigger_navidrome_scan

            intent = result.get("character_adaptation_intent") or {}
            if (intent.get("source_candidate_sha256") == result.get("candidate_sha256")
                    and intent.get("sidecar_path") == result.get("sidecar_path")
                    and intent.get("backup_path")):
                backup = Path(intent["backup_path"])
                if (backup.is_file() and hashlib.sha256(backup.read_bytes()).hexdigest()
                        == result.get("candidate_sha256")):
                    # A killed process cannot run its exception handler. The
                    # durable pre-write intent identifies only our exact
                    # derived content, so a lease retry can restore its input.
                    _rollback_sidecar(intent["sidecar_path"], intent["backup_path"],
                        intent["candidate_sha256"])
            verify_publication()
            service = self.gateway.service
            sidecar = Path(result["sidecar_path"])
            if (sidecar.suffix == ".ttml" and hasattr(service, "backup_existing")
                    and env("AMDL_HAN_CHARACTER_TIMING_ENABLED", "true").lower() in {"true", "1", "yes", "on"}):
                from .character_timing import subdivide_han_word_cues

                normalized, information = subdivide_han_word_cues(sidecar.read_text(encoding="utf-8-sig"))
                if information["changed_groups"]:
                    backup = service.backup_existing(sidecar, Path(job.media_path))
                    if hashlib.sha256(Path(backup).read_bytes()).hexdigest() != result["candidate_sha256"]:
                        raise RuntimeError("lyric changed while preparing character adaptation")
                    result["character_adaptation_intent"] = {
                        "sidecar_path": str(sidecar), "backup_path": backup,
                        "source_candidate_sha256": result["candidate_sha256"],
                        "candidate_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                    }
                    job = self.store.checkpoint_scan_result(job.id, job.lease_token, result=result)
                    verify_publication()
            result = self.character_result(Path(job.media_path), job.result)
            verify_publication()
            if not trigger_navidrome_scan():
                raise RuntimeError("Navidrome scan request was not accepted")
            verify_publication()
            result["navidrome_scan_requested_at"] = time.time()
            self.store.complete(
                job.id,
                job.lease_token,
                result=result,
                expected_statuses={"leased_scan"},
            )
        except Exception as exc:
            # A scan outage must leave the persisted publication retryable.
            # Its character adaptation is cheap to repeat, but its new digest
            # has not been committed to the queue until completion succeeds.
            if (result.get("candidate_sha256") != job.result.get("candidate_sha256")
                    and result.get("sidecar_path") and result.get("candidate_sha256")):
                _rollback_sidecar(result["sidecar_path"], result.get("backup_path", ""),
                    result["candidate_sha256"])
            self.store.fail(
                job.id,
                job.lease_token,
                stage="scan",
                error=f"{type(exc).__name__}:{exc}",
                base_delay_seconds=30,
            )

    def one_cycle(self) -> bool:
        now = time.monotonic()
        if now - self.last_reconcile_at >= self.reconcile_interval:
            recovered = self.store.recover_missing_source_jobs()
            refreshed = self.store.refresh_changed_active_jobs()
            self.last_reconcile_at = now
            if refreshed or recovered:
                self.health(
                    stage="fingerprint_reconciled",
                    refreshed_job_ids=[item.job.id for item in refreshed],
                    recovered_source_jobs=recovered,
                )
        self.store.reclaim_expired()
        for stage, handler in (
            ("validation", self.process_validation),
            ("source", self.process_source),
            ("scan", self.process_scan),
        ):
            job = self.store.claim(
                stage,
                owner=self.owner,
                lease_seconds=self.lease_seconds,
            )
            if job is not None:
                self.health(stage=stage, job_id=job.id)
                handler(job)
                return True
        self.health(stage="idle")
        return False

    def run(self) -> int:
        def stop(*_: Any) -> None:
            self.running = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        self.health(stage="starting")
        while self.running:
            worked = self.one_cycle()
            if not worked:
                time.sleep(self.poll_seconds)
        self.health(stage="stopped")
        return 0


def main() -> int:
    return SourceWorker().run()


if __name__ == "__main__":
    raise SystemExit(main())
