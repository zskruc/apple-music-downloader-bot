from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import time
from pathlib import Path
from typing import Any

from .alignment_backend_whispercpp import acoustic_timeout, run_alignment
from .alignment_retry import reference_language
from .fingerprint import media_fingerprint
from .io import atomic_write_json, atomic_write_text
from .queue import QueueJob, QueueStore
from .text import normalize_text


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def file_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class NasAlignmentWorker:
    def __init__(self) -> None:
        self.state_dir = Path(env("AMDL_WORD_LYRICS_STATE_DIR", "/state/word-lyrics"))
        self.automation_dir = self.state_dir / "automation"
        self.inbox_dir = self.automation_dir / "inbox"
        self.temp_root = self.automation_dir / "nas-alignment-tmp"
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.store = QueueStore(self.automation_dir / "queue.sqlite3")
        self.owner = f"nas-alignment:{socket.gethostname()}:{os.getpid()}"
        self.binary = Path(env("WORD_LYRICS_NAS_WHISPER_BINARY", "/usr/local/bin/whisper-cli"))
        self.model = Path(
            env(
                "WORD_LYRICS_NAS_WHISPER_MODEL",
                str(self.state_dir / "models/ggml-large-v3-turbo-q5_0.bin"),
            )
        )
        self.expected_model_sha1 = env(
            "WORD_LYRICS_NAS_WHISPER_MODEL_SHA1",
            "e050f7970618a659205450ad97eb95a18d69c9ee",
        ).lower()
        self.fallback_model = Path(env(
            "WORD_LYRICS_NAS_FALLBACK_MODEL",
            str(self.state_dir / "models/ggml-large-v3-q5_0.bin"),
        ))
        self.expected_fallback_model_sha1 = "e6e2ed78495d403bef4b7cff42ef4aaadcfea8de"
        self.threads = max(1, int(env("WORD_LYRICS_NAS_THREADS", "4")))
        self.poll_seconds = max(2.0, float(env("WORD_LYRICS_NAS_POLL_SECONDS", "10")))
        self.lease_seconds = max(
            900.0, float(env("WORD_LYRICS_NAS_LEASE_SECONDS", "7200"))
        )
        self.maximum_runs = max(1, int(env("WORD_LYRICS_NAS_MAX_RUNS", "2")))
        self.mac_fallback_enabled = env("WORD_LYRICS_MAC_FALLBACK_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
        self.health_path = self.automation_dir / "nas-alignment-health.json"
        self.running = True
        self.model_sha1 = ""

    def health(self, **extra: Any) -> None:
        atomic_write_json(
            self.health_path,
            {
                "updated_at": time.time(),
                "owner": self.owner,
                "pid": os.getpid(),
                "model": str(self.model),
                "model_sha1": self.model_sha1,
                "threads": self.threads,
                "mac_fallback_enabled": self.mac_fallback_enabled,
                "queue": self.store.stats(),
                **extra,
            },
        )

    def verify_runtime(self) -> None:
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
            raise RuntimeError(f"whisper.cpp binary is unavailable: {self.binary}")
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg is unavailable")
        if not self.model.is_file():
            raise RuntimeError(f"NAS acoustic model is unavailable: {self.model}")
        actual = file_digest(self.model, "sha1")
        if actual != self.expected_model_sha1:
            raise RuntimeError(
                "NAS acoustic model digest mismatch: "
                f"expected={self.expected_model_sha1}, actual={actual}"
            )
        self.model_sha1 = actual

    def alignment_run_count(self, job_id: int) -> int:
        return sum(
            1
            for event in self.store.events(job_id)
            if event["event"] == "claimed"
            and event["detail"].get("stage") == "alignment"
        )

    @staticmethod
    def deterministic_failure(error: str) -> bool:
        return any(
            marker in error
            for marker in (
                "acoustic/reference mismatch",
                "alignment produced",
                "alignment did not preserve",
                "no timed acoustic tokens",
                "reference or acoustic transcript is empty",
                "trusted reference text is required",
            )
        )

    @staticmethod
    def quality_failure(error: str) -> bool:
        return any(marker in error for marker in (
            "acoustic/reference mismatch", "alignment produced",
            "alignment did not preserve", "no timed acoustic tokens",
            "reference or acoustic transcript is empty",
        ))

    @staticmethod
    def fallback_language(payload: dict[str, Any], work: Path) -> str:
        requested = reference_language(payload)
        if requested and requested != "auto":
            return requested
        text = str(payload.get("reference_text") or "")
        if any("\u3400" <= character <= "\u9fff" for character in text):
            # The alternate is an independent Cantonese acoustic candidate;
            # ordinary Chinese auto detection often selects Mandarin instead.
            return "yue"
        try:
            result = json.loads((work / "whole-song.checkpoint.json").read_text())["result"]
            detected = result.get("language")
            if detected:
                return reference_language({"alignment_language": detected}) or "auto"
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return "auto"

    def recover_from_sources(self, job: QueueJob, error: str,
                             diagnostic_payload: dict[str, Any] | None = None) -> bool:
        from .resolver import LegacyGateway, SourceResolutionEngine
        self.store.heartbeat(job.id, job.lease_token, lease_seconds=self.lease_seconds)
        self.health(stage='source_recovery', job_id=job.id)
        engine = SourceResolutionEngine(LegacyGateway(self.state_dir))
        resolution = engine.resolve(
            media_path=Path(job.media_path), metadata=job.metadata,
            expected_fingerprint=job.fingerprint_data)
        if diagnostic_payload is not None:
            observations = []
            try:
                getter = getattr(engine, 'source_observations', None)
                if callable(getter):
                    reported = getter()
                    if isinstance(reported, list):
                        observations = reported
            except Exception:
                # Diagnostics must never prevent a verified source recovery.
                pass
            resolution_payload = (getattr(resolution, 'alignment_payload', None)
                                  or getattr(resolution, 'payload', None) or {})
            if not isinstance(resolution_payload, dict):
                resolution_payload = {}
            diagnostic_payload['post_alignment_source_diagnostics'] = {
                'at': time.time(), 'reason': resolution.reason,
                'observations': observations,
                'rejections': resolution_payload.get('source_rejections', []),
            }
        if resolution.publication is None and resolution.existing is None:
            return False
        result = resolution.publication.as_dict() if resolution.publication is not None else resolution.existing
        self.store.transition_to_scan(job.id, job.lease_token,
            result={**result, 'recovered_after_acoustic_failure': error},
            expected_statuses={'leased_alignment'})
        return True

    def process(self, job: QueueJob) -> None:
        destination: Path | None = None
        work: Path | None = None
        diagnostic_payload = dict(job.payload)
        try:
            media_path = Path(job.media_path)
            if media_fingerprint(media_path) != job.fingerprint_data:
                raise RuntimeError("audio_changed_since_enqueue")
            if env("WORD_LYRICS_MEDIA_INTEGRITY_ENABLED", "true").lower() not in {"false","0","no"}:
                from .media_integrity import validate_media
                validate_media(media_path, job.fingerprint_data, self.automation_dir / "media-integrity",
                    timeout_seconds=float(env("WORD_LYRICS_MEDIA_CHECK_TIMEOUT_SECONDS", "120")))
            duration = float((job.payload.get("track") or {}).get("duration") or 0)
            timeout_seconds = acoustic_timeout(duration)
            # Persist completed inference units across worker restarts and
            # retries. The backend verifies audio/reference/model identities
            # before loading any checkpoint.
            work = self.temp_root / str(job.fingerprint_data["sha256"])
            work.mkdir(parents=True, exist_ok=True)
            candidate = work / "candidate.ttml"
            report_path = work / "report.json"
            acoustic_work = work
            acoustic_stage = "alignment"
            used_model_sha1 = self.model_sha1
            fallback_report: dict[str, Any] = {}

            def progress() -> None:
                if not self.running:
                    raise RuntimeError("worker_stopping")
                self.store.heartbeat(
                    job.id,
                    job.lease_token,
                    lease_seconds=self.lease_seconds,
                )
                try:
                    acoustic_progress = json.loads((acoustic_work / "progress.json").read_text())
                except (OSError, ValueError):
                    acoustic_progress = {}
                self.health(stage=acoustic_stage, job_id=job.id, acoustic_progress=acoustic_progress)

            try:
                report = run_alignment(
                    audio=media_path, payload=job.payload, output=candidate,
                    report_path=report_path, binary=self.binary, model=self.model,
                    work_dir=work, threads=self.threads,
                    timeout_seconds=timeout_seconds, progress=progress,
                )
            except RuntimeError as primary_exception:
                primary_error = f"{type(primary_exception).__name__}:{str(primary_exception)[:600]}"
                if (not self.quality_failure(primary_error)
                        or not normalize_text(str(job.payload.get("reference_text") or ""))):
                    raise
                language = self.fallback_language(job.payload, work)
                fallback_info = {
                    "primary_error": primary_error, "model": str(self.fallback_model),
                    "language": language, "status": "unavailable",
                }
                diagnostic_payload["nas_quality_fallback"] = fallback_info
                if not self.fallback_model.is_file():
                    fallback_info["error"] = "NAS fallback acoustic model is unavailable"
                    raise
                if self.fallback_model.resolve() == self.model.resolve():
                    fallback_info["error"] = "NAS fallback acoustic model is identical to the primary model"
                    raise
                actual_sha1 = file_digest(self.fallback_model, "sha1")
                if actual_sha1 != self.expected_fallback_model_sha1:
                    fallback_info.update(status="invalid_model", actual_sha1=actual_sha1)
                    detail = ("NAS fallback acoustic model digest mismatch: "
                              f"expected={self.expected_fallback_model_sha1}, actual={actual_sha1}")
                    fallback_info["error"] = detail
                    raise RuntimeError(f"{detail}; primary_error={primary_error}") from primary_exception
                if media_fingerprint(media_path) != job.fingerprint_data:
                    raise RuntimeError("audio_changed_before_nas_quality_fallback")
                acoustic_work = work / "quality-fallback"
                acoustic_work.mkdir(parents=True, exist_ok=True)
                acoustic_stage = "quality_fallback"
                candidate = acoustic_work / "candidate.ttml"
                report_path = acoustic_work / "report.json"
                fallback_info.update(status="running", model_sha1=actual_sha1,
                                     checkpoint_directory=str(acoustic_work))
                try:
                    report = run_alignment(
                        audio=media_path,
                        payload={**job.payload, "alignment_language": language},
                        output=candidate, report_path=report_path,
                        binary=self.binary, model=self.fallback_model,
                        work_dir=acoustic_work, threads=self.threads,
                        timeout_seconds=timeout_seconds, progress=progress,
                    )
                except Exception as fallback_exception:
                    fallback_info.update(status="failed", error=(
                        f"{type(fallback_exception).__name__}:{str(fallback_exception)[:1000]}"))
                    raise
                fallback_info["status"] = "completed"
                used_model_sha1 = actual_sha1
                fallback_report = {
                    "primary_error": primary_error, "primary_model": self.model.name,
                    "fallback_model": self.fallback_model.name,
                    "fallback_language": language, "fallback_model_sha1": actual_sha1,
                }
            if media_fingerprint(media_path) != job.fingerprint_data:
                raise RuntimeError("audio_changed_during_nas_alignment")
            report.update(
                {
                    "model_sha1": used_model_sha1,
                    "provider": "nas_whispercpp",
                    **fallback_report,
                }
            )
            atomic_write_json(report_path, report)
            destination = self.inbox_dir / (
                f"job-{job.id}-nas-{os.getpid()}-{int(time.time())}.ttml"
            )
            atomic_write_text(destination, candidate.read_text(encoding="utf-8"))
            self.store.submit_candidate(
                job.id,
                job.lease_token,
                candidate_path=destination,
                report=report,
                stage="alignment",
            )
            destination = None
            # Retain the small transcript checkpoints and report for
            # diagnosis, but release reproducible decoded audio on success.
            for decoded in work.rglob("*.wav"):
                decoded.unlink(missing_ok=True)
        except Exception as exc:
            if destination is not None:
                destination.unlink(missing_ok=True)
            if type(exc).__name__ == "MediaIntegrityError":
                from .faults import classify_exception
                fault=classify_exception(exc,stage="alignment")
                if not fault.retryable:
                    self.store.hold_for_attention(job.id,job.lease_token,stage="alignment",
                        fault=fault.as_dict(),diagnostic_payload=diagnostic_payload)
                else:
                    self.store.fail(job.id,job.lease_token,stage="alignment",error=fault.code,
                        retryable=True,diagnostic_payload=diagnostic_payload)
                self.health(stage="media_integrity_blocked",job_id=job.id,error=fault.code)
                return
            safe_error = f"{type(exc).__name__}:{str(exc)[:1200]}"
            if self.deterministic_failure(safe_error):
                try:
                    if self.recover_from_sources(job, safe_error, diagnostic_payload):
                        return
                    diagnostic_payload['post_alignment_source_recheck'] = 'no_verified_word_source'
                except Exception as recovery_error:
                    diagnostic_payload['post_alignment_source_recheck'] = type(recovery_error).__name__
            if work is not None:
                try:
                    diagnostic_payload["nas_alignment_progress"] = json.loads((work / "progress.json").read_text())
                except (OSError, ValueError):
                    pass
                diagnostic_payload["nas_checkpoint_directory"] = str(work)
                fallback_info = diagnostic_payload.get("nas_quality_fallback")
                if isinstance(fallback_info, dict):
                    try:
                        fallback_info["progress"] = json.loads(
                            (work / "quality-fallback/progress.json").read_text())
                    except (OSError, ValueError):
                        pass
            runs = self.alignment_run_count(job.id)
            terminal = self.deterministic_failure(safe_error) or runs >= self.maximum_runs
            self.store.fail(
                job.id,
                job.lease_token,
                stage="alignment",
                error=safe_error,
                retryable=not terminal,
                offer_mac_on_terminal=terminal and self.mac_fallback_enabled,
                base_delay_seconds=60,
                maximum_delay_seconds=300,
                diagnostic_payload=diagnostic_payload,
            )
            self.health(stage="failed", job_id=job.id, error=safe_error)

    def one_cycle(self) -> bool:
        self.store.reclaim_expired()
        job = self.store.claim(
            "alignment",
            owner=self.owner,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            self.health(stage="idle")
            return False
        self.health(stage="alignment", job_id=job.id)
        self.process(job)
        return True

    def run(self) -> int:
        def stop(*_: Any) -> None:
            self.running = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            self.verify_runtime()
        except Exception as exc:
            self.health(stage="runtime_error", error=f"{type(exc).__name__}:{exc}")
            return 2
        self.health(stage="starting")
        while self.running:
            worked = self.one_cycle()
            if not worked:
                time.sleep(self.poll_seconds)
        self.health(stage="stopped")
        return 0


def main() -> int:
    return NasAlignmentWorker().run()


if __name__ == "__main__":
    raise SystemExit(main())
