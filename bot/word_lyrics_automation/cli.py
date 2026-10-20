from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path

from .io import atomic_write_json, atomic_write_text
from .matching import match_exact_lines
from .providers.netease import fetch_yrc, parse_yrc
from .text import normalize_text
from .ttml import parse_line_references, parse_word_ttml, render_word_ttml


def prepare_netease(args: argparse.Namespace) -> int:
    line_ttml = args.line_ttml.read_text(encoding="utf-8")
    references = parse_line_references(line_ttml)
    if not references:
        raise RuntimeError("the reference TTML has no valid timed lines")
    source_lines = parse_yrc(fetch_yrc(args.netease_id))
    report = match_exact_lines(references, source_lines)
    if report.median_start_delta > args.max_median_start_delta:
        raise RuntimeError("NetEase timing does not match the Apple recording")
    if report.maximum_start_delta > args.max_start_delta:
        raise RuntimeError("NetEase timing has an excessive line-start delta")
    if report.matched_lines[-1].end > args.audio_duration + 0.1:
        raise RuntimeError("NetEase timing extends past the recording")
    reference_text = "".join(reference.text for reference in references)
    matched_text = "".join(line.text for line in report.matched_lines)
    if normalize_text(reference_text) != normalize_text(matched_text):
        raise RuntimeError("matched lyrics do not exactly equal the Apple text")
    ttml = render_word_ttml(
        report.matched_lines,
        duration=args.audio_duration,
        title=args.title,
        artist=args.artist,
        language=args.language,
    )
    parsed = parse_word_ttml(ttml)
    cue_count = sum(len(line.tokens) for line in parsed)
    if len(parsed) != len(references) or cue_count < len(parsed) * 2:
        raise RuntimeError("generated TTML failed the word-timing sanity check")
    token_durations = [
        token.end - token.begin for line in parsed for token in line.tokens
    ]
    short_tokens = sum(duration < 0.08 for duration in token_durations)
    metrics = {
        "status": "accepted",
        "provider": "netease_yrc",
        "provider_song_id": args.netease_id,
        "apple_song_id": args.apple_id,
        "audio_duration_seconds": round(args.audio_duration, 6),
        "reference_line_count": len(references),
        "source_line_count": len(source_lines),
        "matched_line_count": len(parsed),
        "skipped_source_indexes": list(report.skipped_source_indexes),
        "cue_count": cue_count,
        "normalized_reference_characters": len(normalize_text(reference_text)),
        "text_similarity": 1.0,
        "median_start_delta_seconds": round(report.median_start_delta, 6),
        "maximum_start_delta_seconds": round(report.maximum_start_delta, 6),
        "median_end_delta_seconds": round(statistics.median(report.end_deltas), 6),
        "maximum_end_seconds": round(parsed[-1].end, 6),
        "zero_duration_token_count": 0,
        "shorter_than_80ms_token_count": short_tokens,
        "shorter_than_80ms_token_fraction": round(short_tokens / cue_count, 6),
        "ttml_sha256": hashlib.sha256(ttml.encode("utf-8")).hexdigest(),
    }
    atomic_write_text(args.output, ttml)
    atomic_write_json(args.report, metrics)
    print(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-netease")
    prepare.add_argument("--netease-id", required=True)
    prepare.add_argument("--apple-id", default="")
    prepare.add_argument("--line-ttml", type=Path, required=True)
    prepare.add_argument("--audio-duration", type=float, required=True)
    prepare.add_argument("--title", required=True)
    prepare.add_argument("--artist", required=True)
    prepare.add_argument("--language", default="zh-Hans")
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--report", type=Path, required=True)
    prepare.add_argument("--max-median-start-delta", type=float, default=0.4)
    prepare.add_argument("--max-start-delta", type=float, default=1.0)
    prepare.set_defaults(func=prepare_netease)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
