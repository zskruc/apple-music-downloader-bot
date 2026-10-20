from __future__ import annotations

import argparse
import json
from pathlib import Path

from .alignment_retry import build_with_reference_retry, reference_language
from .io import atomic_write_json, atomic_write_text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    import mlx_whisper

    payload = json.loads(args.payload.read_text(encoding="utf-8"))
    reference_text = str(payload.get("reference_text") or "")
    result = mlx_whisper.transcribe(
        str(args.audio),
        path_or_hf_repo=args.model,
        word_timestamps=True,
        verbose=False,
        temperature=0.0,
        condition_on_previous_text=False,
        initial_prompt=reference_text[:1200] or None,
        hallucination_silence_threshold=1.5,
        language=reference_language(payload),
    )
    samples = None

    def transcribe_window(begin: float, end: float, text: str, language: str | None) -> dict:
        nonlocal samples
        from mlx_whisper.audio import SAMPLE_RATE, load_audio
        if samples is None:
            samples = load_audio(str(args.audio))
        return mlx_whisper.transcribe(
            samples[round(begin * SAMPLE_RATE):round(end * SAMPLE_RATE)],
            path_or_hf_repo=args.model, word_timestamps=True, verbose=False,
            temperature=0.0, condition_on_previous_text=False,
            initial_prompt=text, language=language,
        )

    ttml, report = build_with_reference_retry(result, payload, transcribe_window)
    report.update(
        {
            "model": args.model,
            "language": result.get("language"),
            "source_fingerprint": payload.get("source_fingerprint"),
            "reference_provider": payload.get("reference_provider"),
            "reference_provider_id": payload.get("reference_provider_id"),
            "reference_path": payload.get("reference_path"),
        }
    )
    atomic_write_text(args.output, ttml)
    atomic_write_json(args.report, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
