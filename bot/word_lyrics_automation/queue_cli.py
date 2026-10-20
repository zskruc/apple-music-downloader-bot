from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

from .io import atomic_write_text
from .queue import QueueStore


def state_dir() -> Path:
    return Path(os.environ.get("AMDL_WORD_LYRICS_STATE_DIR", "/state/word-lyrics"))


def store() -> QueueStore:
    return QueueStore(state_dir() / "automation" / "queue.sqlite3")


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def command_claim(args: argparse.Namespace) -> int:
    job = store().claim(
        args.stage,
        owner=args.owner,
        lease_seconds=args.lease_seconds,
    )
    print_json(job.as_dict() if job else None)
    return 0


def command_heartbeat(args: argparse.Namespace) -> int:
    job = store().heartbeat(args.job_id, args.token, lease_seconds=args.lease_seconds)
    print_json(job.as_dict())
    return 0


def command_fail(args: argparse.Namespace) -> int:
    job = store().fail(
        args.job_id,
        args.token,
        stage=args.stage,
        error=args.error,
        retryable=not args.permanent,
        offer_mac_on_terminal=args.offer_mac_on_terminal,
    )
    print_json(job.as_dict())
    return 0


def command_stats(_: argparse.Namespace) -> int:
    print_json(store().stats())
    return 0


def command_retry_source(args: argparse.Namespace) -> int:
    job = store().request_source_recheck(args.job_id, reason=args.reason)
    print_json({"job_id": job.id, "status": job.status, "next_attempt_at": job.next_attempt_at})
    return 0


def command_job(args: argparse.Namespace) -> int:
    queue = store()
    job = queue.get(args.job_id)
    print_json(
        {
            "job": job.as_dict() if job else None,
            "events": queue.events(args.job_id) if job else [],
        }
    )
    return 0


def command_health(args: argparse.Namespace) -> int:
    path = state_dir() / "automation" / "source-worker-health.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(payload.get("updated_at") or 0)
    except Exception:
        return 1
    if age > args.max_age or payload.get("queue", {}).get("quick_check") != "ok":
        return 1
    print_json({"status": "ok", "age_seconds": round(age, 3)})
    return 0


def command_export_audio(args: argparse.Namespace) -> int:
    queue = store()
    job = queue.verify_lease(args.job_id, args.token, stage=args.stage)
    path = Path(job.media_path)
    if not path.is_file() or path.stat().st_size > args.max_bytes:
        raise RuntimeError("leased audio is missing or exceeds the export limit")
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
    return 0


def command_submit_candidate(args: argparse.Namespace) -> int:
    queue = store()
    queue.verify_lease(args.job_id, args.token, stage=args.stage)
    report = json.loads(base64.urlsafe_b64decode(args.report_b64 + "===").decode("utf-8"))
    content = sys.stdin.buffer.read(args.max_bytes + 1)
    if not content or len(content) > args.max_bytes:
        raise RuntimeError("candidate is empty or exceeds the upload limit")
    text = content.decode("utf-8", errors="strict")
    inbox = state_dir() / "automation" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    destination = inbox / f"job-{args.job_id}-{os.getpid()}-{int(time.time())}.ttml"
    atomic_write_text(destination, text)
    try:
        job = queue.submit_candidate(
            args.job_id,
            args.token,
            candidate_path=destination,
            report=report,
            stage=args.stage,
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    print_json(job.as_dict())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    claim = commands.add_parser("claim")
    claim.add_argument(
        "--stage",
        choices=("source", "alignment", "mac_alignment", "validation", "scan"),
        required=True,
    )
    claim.add_argument("--owner", required=True)
    claim.add_argument("--lease-seconds", type=float, default=2700)
    claim.set_defaults(func=command_claim)
    heartbeat = commands.add_parser("heartbeat")
    heartbeat.add_argument("--job-id", type=int, required=True)
    heartbeat.add_argument("--token", required=True)
    heartbeat.add_argument("--lease-seconds", type=float, default=2700)
    heartbeat.set_defaults(func=command_heartbeat)
    fail = commands.add_parser("fail")
    fail.add_argument("--job-id", type=int, required=True)
    fail.add_argument("--token", required=True)
    fail.add_argument(
        "--stage",
        choices=("source", "alignment", "mac_alignment", "validation", "scan"),
        required=True,
    )
    fail.add_argument("--error", required=True)
    fail.add_argument("--permanent", action="store_true")
    fail.add_argument("--offer-mac-on-terminal", action="store_true")
    fail.set_defaults(func=command_fail)
    stats = commands.add_parser("stats")
    stats.set_defaults(func=command_stats)
    retry_source = commands.add_parser("retry-source")
    retry_source.add_argument("--job-id", type=int, required=True)
    retry_source.add_argument("--reason", required=True)
    retry_source.set_defaults(func=command_retry_source)
    job = commands.add_parser("job")
    job.add_argument("--job-id", type=int, required=True)
    job.set_defaults(func=command_job)
    health = commands.add_parser("health")
    health.add_argument("--max-age", type=float, default=90)
    health.set_defaults(func=command_health)
    export = commands.add_parser("export-audio")
    export.add_argument("--job-id", type=int, required=True)
    export.add_argument("--token", required=True)
    export.add_argument(
        "--stage", choices=("alignment", "mac_alignment"), default="alignment"
    )
    export.add_argument("--max-bytes", type=int, default=2 * 1024 * 1024 * 1024)
    export.set_defaults(func=command_export_audio)
    submit = commands.add_parser("submit-candidate")
    submit.add_argument("--job-id", type=int, required=True)
    submit.add_argument("--token", required=True)
    submit.add_argument(
        "--stage", choices=("alignment", "mac_alignment"), default="alignment"
    )
    submit.add_argument("--report-b64", required=True)
    submit.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    submit.set_defaults(func=command_submit_candidate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
