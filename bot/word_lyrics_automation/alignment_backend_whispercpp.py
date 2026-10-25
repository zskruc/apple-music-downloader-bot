from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Callable

from .alignment_retry import build_with_reference_retry, reference_language
from .alignment import (_parallel_agent_pairs, _reference_lines, reference_crop_bounds,
                        validate_reference_payload)
from .fingerprint import media_fingerprint, sha256_file
from .io import atomic_write_json, atomic_write_text
from .text import normalize_text
from .whisper_dtw import NoTimedAcousticTokensError, convert_dtw_document


ProgressCallback = Callable[[], None]


_DTW_CLOCK_METHOD = "whisper_dtw_group_boundaries_v1"
_MODEL_HEADER_FIELDS = (
    "vocab", "audio_ctx", "audio_state", "audio_head", "audio_layer",
    "text_ctx", "text_state", "text_head", "text_layer", "mels",
)
# These are the actual GGML architectures of the two installed multilingual
# models. A filename cannot establish which attention-head preset is valid.
_DTW_MODEL_PRESETS = {
    (51866, 1500, 1280, 20, 32, 448, 1280, 20, 4, 128): "large.v3.turbo",
    (51866, 1500, 1280, 20, 32, 448, 1280, 20, 32, 128): "large.v3",
}


def read_dtw_model_metadata(model: Path) -> dict[str, Any]:
    """Select only an unambiguous DTW preset from the GGML file header."""
    try:
        with model.open("rb") as handle:
            header = handle.read(48)
    except OSError as exc:
        raise RuntimeError(f"cannot read whisper DTW model header: {exc}") from exc
    if len(header) != 48:
        raise RuntimeError("unsupported whisper DTW model: truncated GGML header")
    magic, *values = struct.unpack("<12I", header)
    if magic != 0x67676D6C:
        raise RuntimeError("unsupported whisper DTW model: invalid GGML magic")
    architecture = tuple(values[:10])
    preset = _DTW_MODEL_PRESETS.get(architecture)
    if preset is None:
        details = dict(zip(_MODEL_HEADER_FIELDS, architecture))
        raise RuntimeError(f"unsupported whisper DTW model architecture: {details}")
    return {
        "preset": preset,
        "hyperparameters": dict(zip(_MODEL_HEADER_FIELDS, architecture)),
        "ggml_ftype": values[10],
        "header_sha256": hashlib.sha256(header).hexdigest(),
    }


def _validate_dtw_document_model(document: dict[str, Any], metadata: dict[str, Any]) -> None:
    actual = document.get("model")
    if not isinstance(actual, dict):
        raise RuntimeError("whisper DTW output is missing actual model hyperparameters")
    observed = {"vocab": actual.get("vocab"), "mels": actual.get("mels")}
    for kind in ("audio", "text"):
        section = actual.get(kind)
        if not isinstance(section, dict):
            raise RuntimeError("whisper DTW output is missing actual model hyperparameters")
        for field in ("ctx", "state", "head", "layer"):
            observed[f"{kind}_{field}"] = section.get(field)
    if any(type(observed.get(key)) is not int or observed[key] != value
           for key, value in metadata["hyperparameters"].items()):
        raise RuntimeError("whisper DTW output model does not match the verified GGML architecture")


def _dtw_clock_diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    diagnostics = result.get("clock_diagnostics")
    if (not isinstance(diagnostics, dict)
            or diagnostics.get("method") != _DTW_CLOCK_METHOD
            or diagnostics.get("derived_group_end") is not True):
        raise RuntimeError("whisper DTW result is missing verified clock diagnostics")
    for key in ("timed_unit_count", "untimed_unit_count", "timed_character_count", "untimed_character_count"):
        if type(diagnostics.get(key)) is not int or diagnostics[key] < 0:
            raise RuntimeError("whisper DTW result has invalid clock diagnostic counts")
    return diagnostics


def validate_dtw_result_clock(
    result: dict[str, Any], audio: Path, *,
    empty_observation: NoTimedAcousticTokensError | None = None,
) -> None:
    """Reject bad clocks before the matcher can clamp or discard words."""
    try:
        with wave.open(str(audio), "rb") as source:
            frames, rate = source.getnframes(), source.getframerate()
    except (OSError, EOFError, wave.Error) as exc:
        raise RuntimeError(f"cannot verify whisper DTW audio crop: {exc}") from exc
    if rate <= 0 or frames <= 0:
        raise RuntimeError("cannot verify whisper DTW clock against empty audio crop")
    duration = frames / rate
    if empty_observation is not None:
        if (not isinstance(empty_observation, NoTimedAcousticTokensError)
                or empty_observation.result is not result):
            raise RuntimeError("whisper DTW empty observation is not a verified converter result")
        diagnostics = _dtw_clock_diagnostics(result)
        if diagnostics["timed_unit_count"] or diagnostics["timed_character_count"]:
            raise RuntimeError("whisper DTW empty observation contains timed lexical units")
        bounds = empty_observation.anchor_bounds
        if bounds is not None:
            try:
                valid = (isinstance(bounds, tuple) and len(bounds) == 2
                         and all(not isinstance(value, bool) and isinstance(value, (int, float))
                                 and math.isfinite(value) for value in bounds)
                         and 0 <= bounds[0] <= bounds[1])
            except OverflowError:
                valid = False
            if not valid:
                raise RuntimeError("whisper DTW empty observation has invalid anchor bounds")
            if bounds[1] > duration + 0.020 + 1e-9:
                raise RuntimeError("whisper DTW clock exceeds its actual audio crop")
        elif diagnostics.get("raw_text_token_count"):
            raise RuntimeError("whisper DTW empty observation is missing text-token anchor bounds")
    previous_end: float | None = None
    word_count = 0
    segments = result.get("segments")
    if not isinstance(segments, list):
        raise RuntimeError("whisper DTW clock is missing word segments")
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("words"), list):
            raise RuntimeError("whisper DTW clock has invalid word segments")
        for word in segment["words"]:
            if not isinstance(word, dict):
                raise RuntimeError("whisper DTW clock has an invalid word")
            begin, end = word.get("start"), word.get("end")
            try:
                finite = all(not isinstance(value, bool) and isinstance(value, (int, float))
                             and math.isfinite(value) for value in (begin, end))
            except OverflowError:
                finite = False
            if not finite:
                raise RuntimeError("whisper DTW clock has non-finite or invalid word bounds")
            if begin < 0 or end <= begin:
                raise RuntimeError("whisper DTW clock has non-positive word bounds")
            # DTW advances in 20 ms frames; permit one boundary frame beyond
            # the exact decoded WAV duration, never a native-line duration.
            if end > duration + 0.020 + 1e-9:
                raise RuntimeError("whisper DTW clock exceeds its actual audio crop")
            if previous_end is not None and begin < previous_end - 0.001 - 1e-9:
                raise RuntimeError("whisper DTW clock overlaps or reverses across word segments")
            previous_end = end
            word_count += 1
    if word_count and empty_observation is not None:
        raise RuntimeError("whisper DTW empty observation contains timed acoustic words")
    if not word_count and empty_observation is None:
        raise RuntimeError("whisper DTW clock returned no timed acoustic words")


def acoustic_timeout(duration: float) -> float:
    """Budget each inference separately on a CPU-only NAS.

    Short crops still require the model's padded encoder pass. A whole-song
    wall-clock budget must not be shared by all subsequent line retries.
    """
    return max(1800.0, duration * 60.0 + 1800.0)


def load_whispercpp_document(path: Path) -> dict[str, Any]:
    # whisper.cpp's full JSON writer can split one UTF-8 character across two
    # token strings.  The resulting byte stream is valid JSON structurally but
    # is not valid UTF-8 at each individual string boundary.  Surrogate escape
    # preserves those bytes so the token converter below can reassemble them
    # without replacing or inventing lyric characters.
    raw = path.read_bytes()
    document = json.loads(raw.decode("utf-8", errors="surrogateescape"))
    if not isinstance(document, dict):
        raise RuntimeError("whisper.cpp returned a non-object JSON document")
    return document


def _run_with_progress(
    arguments: list[str],
    *,
    timeout_seconds: float,
    progress: ProgressCallback | None = None,
) -> None:
    process = subprocess.Popen(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()

    def stop_process() -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    while True:
        try:
            stdout, stderr = process.communicate(timeout=30)
            break
        except subprocess.TimeoutExpired:
            if time.monotonic() - started >= timeout_seconds:
                stop_process()
                raise RuntimeError("whisper.cpp alignment timed out")
            if progress is not None:
                try:
                    progress()
                except Exception:
                    stop_process()
                    raise
    if process.returncode:
        detail = (stderr or stdout or "").strip()[-1500:]
        raise RuntimeError(
            f"whisper.cpp exited with code {process.returncode}: {detail}"
        )


def whispercpp_document_to_result(document: dict[str, Any]) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    for raw_segment in document.get("transcription") or []:
        if not isinstance(raw_segment, dict):
            continue
        words: list[dict[str, Any]] = []
        pending = bytearray()
        pending_offsets: list[tuple[float, float]] = []
        pending_probabilities: list[float] = []

        def flush_pending(*, final: bool = False) -> None:
            if not pending:
                return
            try:
                decoded = bytes(pending).decode("utf-8")
            except UnicodeDecodeError as exc:
                if not final and exc.reason == "unexpected end of data":
                    return
                raise RuntimeError(
                    "whisper.cpp returned an unrecoverable split UTF-8 token"
                ) from exc
            begin = min(value[0] for value in pending_offsets)
            end = max(value[1] for value in pending_offsets)
            if normalize_text(decoded) and begin >= 0 and end > begin:
                words.append(
                    {
                        "word": decoded,
                        "start": begin,
                        "end": end,
                        "probability": sum(pending_probabilities)
                        / len(pending_probabilities),
                    }
                )
            pending.clear()
            pending_offsets.clear()
            pending_probabilities.clear()

        for raw_token in raw_segment.get("tokens") or []:
            if not isinstance(raw_token, dict):
                continue
            text = str(raw_token.get("text") or "")
            stripped = text.strip()
            if (
                (stripped.startswith("<|") and stripped.endswith("|>"))
                or (stripped.startswith("[_") and stripped.endswith("]"))
            ):
                continue
            offsets = raw_token.get("offsets")
            if not isinstance(offsets, dict):
                continue
            try:
                begin = float(offsets.get("from")) / 1000.0
                end = float(offsets.get("to")) / 1000.0
                probability = float(raw_token.get("p") or 0.0)
            except (TypeError, ValueError):
                continue
            if begin < 0 or end < begin:
                continue
            try:
                token_bytes = text.encode("utf-8", errors="surrogateescape")
            except UnicodeEncodeError as exc:
                raise RuntimeError(
                    "whisper.cpp returned an unsupported surrogate token"
                ) from exc
            if not token_bytes:
                continue
            pending.extend(token_bytes)
            pending_offsets.append((begin, end))
            pending_probabilities.append(probability)
            flush_pending()
        flush_pending(final=True)
        if words:
            segments.append({"words": words})
    if not segments:
        raise RuntimeError("whisper.cpp returned no timed acoustic tokens")
    result = document.get("result")
    language = str(result.get("language") or "") if isinstance(result, dict) else ""
    return {"language": language, "segments": segments}


def run_alignment(
    *,
    audio: Path,
    payload: dict[str, Any],
    output: Path,
    report_path: Path,
    binary: Path,
    model: Path,
    work_dir: Path,
    threads: int,
    timeout_seconds: float,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    validate_reference_payload(payload)
    language = reference_language(payload)
    work_dir.mkdir(parents=True, exist_ok=True)
    actual_fingerprint = media_fingerprint(audio)
    if payload.get("source_fingerprint") and payload["source_fingerprint"] != actual_fingerprint:
        raise RuntimeError("audio_changed_since_enqueue")
    dtw_model = read_dtw_model_metadata(model)
    # The checkpoint key excludes queue IDs so a requeued job can resume,
    # but includes everything that changes acoustic inference or reference
    # matching. Never reuse timestamps for different audio or lyric text.
    context = {
        "version": 2,
        "audio": actual_fingerprint,
        "reference_text": payload.get("reference_text"),
        "reference_lines": payload.get("reference_lines"),
        "duration": (payload.get("track") or {}).get("duration"),
        "model": sha256_file(model) if model.is_file() else str(model),
        "binary": sha256_file(binary) if binary.is_file() else str(binary),
        "threads": threads,
        "clock_method": _DTW_CLOCK_METHOD,
        "dtw_model": dtw_model,
    }
    # Preserve keys for existing default-language checkpoints, while keeping
    # explicit language candidates separate even when every other input agrees.
    if "alignment_language" in payload:
        context["alignment_language"] = language
    context_key = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    atomic_write_json(work_dir / "checkpoint-context.json", {"key": context_key, **context})
    completed_units: list[str] = []
    reused_units: list[str] = []
    zero_evidence_units: list[str] = []
    clock_diagnostics_by_unit: dict[str, dict[str, Any]] = {}

    def unit_progress(stage: str, **extra: Any) -> None:
        atomic_write_json(work_dir / "progress.json", {
            "updated_at": time.time(), "stage": stage,
            "completed_units": completed_units, "reused_units": reused_units,
            "zero_evidence_units": zero_evidence_units,
            "reference_lines": len(payload.get("reference_lines") or []),
            **extra,
        })
        if progress is not None:
            progress()

    def transcribe_cached(arguments: list[str], unit: str, budget: float) -> dict[str, Any]:
        checkpoint = work_dir / f"{unit}.checkpoint.json"
        unit_audio = Path(arguments[arguments.index("-f") + 1])
        key = hashlib.sha256(json.dumps([context_key, arguments], sort_keys=True).encode()).hexdigest()
        try:
            saved = json.loads(checkpoint.read_text())
            if saved.get("key") == key and isinstance(saved.get("result"), dict):
                diagnostics = _dtw_clock_diagnostics(saved["result"])
                validate_dtw_result_clock(saved["result"], unit_audio)
                clock_diagnostics_by_unit[unit] = diagnostics
                completed_units.append(unit)
                reused_units.append(unit)
                unit_progress("resuming", unit=unit)
                return saved["result"]
        except (OSError, ValueError, AttributeError, RuntimeError):
            pass
        started = time.monotonic()

        def running() -> None:
            unit_progress("recognizing", unit=unit,
                          elapsed_seconds=round(time.monotonic() - started),
                          timeout_seconds=budget)

        prefix = Path(arguments[arguments.index("-of") + 1])
        # Raw output alone is not a committed checkpoint: a killed process
        # can leave an incomplete JSON file behind.
        prefix.with_suffix(".json").unlink(missing_ok=True)
        running()
        try:
            _run_with_progress(arguments, timeout_seconds=budget, progress=running)
            document = load_whispercpp_document(prefix.with_suffix(".json"))
            _validate_dtw_document_model(document, dtw_model)
            # Legacy offsets can be uniformly spread over an entire segment.
            # Production must receive real DTW anchors and never silently fall
            # back to those offsets when DTW was disabled or malformed.
            empty_observation: NoTimedAcousticTokensError | None = None
            try:
                result = convert_dtw_document(document)
            except NoTimedAcousticTokensError as exc:
                # A valid observation can contain only one terminal anchor
                # (or no lexical text). Keep its zero coverage and retry the
                # other trusted windows; do not manufacture a terminal end.
                result = exc.result
                empty_observation = exc
            validate_dtw_result_clock(result, unit_audio, empty_observation=empty_observation)
            clock_diagnostics_by_unit[unit] = _dtw_clock_diagnostics(result)
        except Exception as exc:
            unit_progress("interrupted", unit=unit, error=f"{type(exc).__name__}:{str(exc)[:1000]}")
            raise
        if empty_observation is not None:
            # This is diagnostic evidence, not a successful acoustic
            # checkpoint. In particular it cannot bypass publication gates
            # on a later run by masquerading as completed timing data.
            zero_evidence_units.append(unit)
            unit_progress("no_timed_evidence", unit=unit)
            return result
        atomic_write_json(checkpoint, {"key": key, "result": result})
        completed_units.append(unit)
        unit_progress("checkpoint_saved", unit=unit)
        return result

    wav = work_dir / "source-16khz-mono.wav"
    transcript_prefix = work_dir / "whisper-result"
    track = payload.get("track")
    duration = float(track.get("duration") or 0) if isinstance(track, dict) else 0.0
    ffmpeg_timeout = max(300.0, duration * 2.0)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(audio),
                "-vn",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(wav),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=ffmpeg_timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg audio preparation failed: {exc.stderr[-1000:]}") from exc

    reference_text = str(payload.get("reference_text") or "").strip()
    # whisper.cpp 4979e04f gates initial_prompt on n_max_text_ctx > 0 too.
    # Retain its no_context=true default and a fresh CLI process per unit;
    # the bounded prompt must not enable history across separate audio units.
    arguments = [
        str(binary),
        "-m",
        str(model),
        "-f",
        str(wav),
        "-t",
        str(max(1, threads)),
        "-p",
        "1",
        "-l",
        language or "auto",
        "-bs",
        "5",
        "-bo",
        "5",
        "-tp",
        "0",
        "-wt",
        "0.01",
        "-mc",
        "128" if reference_text else "0",
        "-ml",
        "64",
        "-sow",
        "-ojf",
        "-of",
        str(transcript_prefix),
        "-np",
        "-ng",
        "-nfa",
        "-dtw",
        dtw_model["preset"],
    ]
    if reference_text:
        arguments.extend(["--prompt", reference_text[:1200]])
    # A mixed whole-song transcript cannot certify overlapping vocal agents.
    # These references must use independent crops, so avoid an inference that
    # the matcher would necessarily reject before doing those same crops.
    independent_parallel_windows = bool(
        duration > 0 and payload.get("reference_lines")
        and _parallel_agent_pairs(_reference_lines(payload, duration)))
    if independent_parallel_windows:
        result = {"language": language, "segments": []}
        unit_progress("independent_parallel_line_windows")
    else:
        result = transcribe_cached(arguments, "whole-song", timeout_seconds)
    retry_index = 0
    reference_cursor = 0
    retry_references = _reference_lines(payload, duration) if duration > 0 else []

    def transcribe_window(begin: float, end: float, text: str, language: str | None) -> dict[str, Any]:
        nonlocal retry_index, reference_cursor
        # Skipped humming rows must not renumber existing checkpoints. Match
        # the original reference ordinal, preserving old audio/argv keys.
        # Match forward in the original document, rather than repeatedly
        # picking its first equal text/start. Parallel voices can legitimately
        # share both; each still needs its own stable ordinal checkpoint.
        references = retry_references
        for index in range(reference_cursor, len(references)):
            reference = references[index]
            if (reference.get('text') == text
                    and abs(reference_crop_bounds(references, index, duration)[0] - begin) < 1e-6
                    and abs(reference_crop_bounds(references, index, duration)[1] - end) < 1e-6):
                retry_index = index + 1
                reference_cursor = index + 1
                break
        else:
            raise RuntimeError("retry crop does not match the next reference line")
        cropped = work_dir / f"line-{retry_index}.wav"
        prefix = work_dir / f"line-{retry_index}"
        with wave.open(str(wav), "rb") as source:
            rate = source.getframerate()
            source.setpos(min(source.getnframes(), round(begin * rate)))
            frames = source.readframes(round((end - begin) * rate))
            with wave.open(str(cropped), "wb") as target:
                target.setparams(source.getparams())
                target.writeframes(frames)
        retry_arguments = list(arguments)
        for option, value in (("-f", str(cropped)), ("-of", str(prefix)),
                              ("-l", language or "auto"), ("-mc", "128" if text.strip() else "0")):
            retry_arguments[retry_arguments.index(option) + 1] = value
        if "--prompt" in retry_arguments:
            retry_arguments[retry_arguments.index("--prompt") + 1] = text
        else:
            retry_arguments.extend(["--prompt", text])
        return transcribe_cached(retry_arguments, f"line-{retry_index}", acoustic_timeout(end - begin))

    ttml, report = build_with_reference_retry(result, payload, transcribe_window)
    report.update(
        {
            "alignment_engine": "whisper.cpp",
            "model": model.name,
            "language": result.get("language"),
            "alignment_language": language or "auto",
            "whole_song_inference_skipped": independent_parallel_windows,
            "clock_method": _DTW_CLOCK_METHOD,
            "clock_group_end_derived": True,
            "reference_interpolation": "estimated_within_verified_native_windows",
            "dtw_model": dtw_model,
            "clock_diagnostics": {
                "method": _DTW_CLOCK_METHOD,
                "derived_group_end": True,
                "end_semantics": "next_distinct_text_token_anchor_not_full_pronunciation",
                "reference_interpolation": "estimated_within_verified_native_windows",
                "count_scope": "all_acoustic_units_before_reference_selection",
                "units": clock_diagnostics_by_unit,
                "untimed_group_count": sum(
                    value.get("untimed_group_count", 0)
                    for value in clock_diagnostics_by_unit.values()
                ),
                "untimed_unit_count": sum(
                    value.get("untimed_unit_count", 0)
                    for value in clock_diagnostics_by_unit.values()
                ),
                "untimed_character_count": sum(
                    value.get("untimed_character_count", 0)
                    for value in clock_diagnostics_by_unit.values()
                ),
            },
            "source_fingerprint": payload.get("source_fingerprint"),
            "reference_provider": payload.get("reference_provider"),
            "reference_provider_id": payload.get("reference_provider_id"),
            "reference_path": payload.get("reference_path"),
            "checkpoint_key": context_key,
            "completed_acoustic_units": len(completed_units),
            "reused_acoustic_units": len(reused_units),
            "zero_evidence_acoustic_units": zero_evidence_units,
        }
    )
    atomic_write_text(output, ttml)
    atomic_write_json(report_path, report)
    unit_progress("completed")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=7200)
    args = parser.parse_args()
    payload = json.loads(args.payload.read_text(encoding="utf-8"))
    run_alignment(
        audio=args.audio,
        payload=payload,
        output=args.output,
        report_path=args.report,
        binary=args.binary,
        model=args.model,
        work_dir=args.work_dir,
        threads=args.threads,
        timeout_seconds=args.timeout_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
