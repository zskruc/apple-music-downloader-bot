"""Bounded, read-only checks of the media bytes used by a lyric job.

FLAC has an independent native sample count and sometimes a PCM checksum;
those are checked explicitly because ffmpeg can exit zero for a short FLAC.
For the other supported containers, a clean full decode is evidence of
decodability, not proof that the upstream recording is complete.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Callable


CHECKER_VERSION = "media-integrity-v1"
_CHUNK = 64 * 1024
_CACHEABLE_ERRORS = {"media_corrupt", "media_header_mismatch", "media_checksum_mismatch"}
_PCM = {8: ("pcm_s8", "s8"), 16: ("pcm_s16le", "s16le"),
        24: ("pcm_s24le", "s24le"), 32: ("pcm_s32le", "s32le")}


class MediaIntegrityError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "retryable": self.retryable, "details": copy.deepcopy(self.details)}


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise MediaIntegrityError("media_integrity_timeout", retryable=True)
    return value


def _fingerprint(path: Path, deadline: float, maximum: int) -> dict[str, Any]:
    try:
        before = path.stat()
        if not path.is_file():
            raise MediaIntegrityError("media_unsupported", details={"reason": "not_a_regular_file"})
        if before.st_size > maximum:
            raise MediaIntegrityError("media_resource_limit", details={"reason": "input_size", "size": before.st_size, "limit": maximum})
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while block := handle.read(_CHUNK):
                _remaining(deadline)
                size += len(block)
                if size > maximum:
                    raise MediaIntegrityError("media_resource_limit", details={"reason": "input_size", "limit": maximum})
                digest.update(block)
        after = path.stat()
    except OSError as exc:
        raise MediaIntegrityError("media_io_error", retryable=True, details={"exception": type(exc).__name__}) from exc
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or size != after.st_size:
        raise MediaIntegrityError("media_changed", retryable=True, details={"phase": "fingerprinting"})
    return {"sha256": digest.hexdigest(), "size": size, "mtime_ns": after.st_mtime_ns}


def _check_expected(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    if actual != expected:
        raise MediaIntegrityError("media_changed", retryable=True,
                                  details={"phase": "fingerprint_binding", "actual_fingerprint": actual,
                                           "expected_fingerprint": expected})


def _tool(value: str | Path) -> str:
    found = shutil.which(str(value))
    if found is None:
        raise MediaIntegrityError("media_tool_unavailable", retryable=True,
                                  details={"tool": Path(value).name})
    return found


def _run(argv: list[str], deadline: float, consume: Callable[[bytes], None], *, stderr_limit: int = 16 * 1024) -> tuple[int, str]:
    """Drain both pipes without buffering PCM or blocking beyond the deadline."""
    process: subprocess.Popen[bytes] | None = None
    tail = bytearray()
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        assert process.stdout is not None and process.stderr is not None
        with selectors.DefaultSelector() as selector:
            for stream, kind in ((process.stdout, "pcm"), (process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, kind)
            while selector.get_map():
                events = selector.select(min(.20, _remaining(deadline)))
                for key, _ in events:
                    try:
                        block = os.read(key.fileobj.fileno(), _CHUNK)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(key.fileobj)
                    elif key.data == "pcm":
                        consume(block)
                    else:
                        tail.extend(block)
                        del tail[:-stderr_limit]
            message = tail.decode("utf-8", errors="replace").strip()
            # A damaged container can reference a credential-bearing URL.
            # Preserve the decoder error, but never copy that URL into state.
            message = re.sub(r"(?i)(https?|rtsp|rtmp|ftp|sftp)://[^\s\"'<>]+", r"\1://[redacted]", message)
            return process.wait(timeout=_remaining(deadline)), message
    except subprocess.TimeoutExpired as exc:
        raise MediaIntegrityError("media_integrity_timeout", retryable=True) from exc
    except FileNotFoundError as exc:
        raise MediaIntegrityError("media_tool_unavailable", retryable=True,
                                  details={"tool": Path(argv[0]).name}) from exc
    except OSError as exc:
        raise MediaIntegrityError("media_io_error", retryable=True,
                                  details={"exception": type(exc).__name__}) from exc
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def _probe(path: Path, ffprobe: str, deadline: float) -> tuple[dict[str, Any], str]:
    chunks = bytearray()

    def collect(block: bytes) -> None:
        chunks.extend(block)
        if len(chunks) > 1024 * 1024:
            raise MediaIntegrityError("media_resource_limit", details={"reason": "probe_output"})

    code, error = _run([ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "a",
                        "-show_entries", "stream=index,codec_name,sample_rate,channels,bits_per_sample,bits_per_raw_sample:format=format_name",
                        "-of", "json", str(path)], deadline, collect)
    if code or error:
        raise MediaIntegrityError("media_corrupt", details={"phase": "probe", "returncode": code, "decoder_message": error})
    try:
        document = json.loads(chunks)
        streams = document["streams"]
        formats = str(document["format"]["format_name"]).split(",")
        if not isinstance(streams, list):
            raise ValueError("streams are not an array")
    except (ValueError, KeyError, TypeError) as exc:
        raise MediaIntegrityError("media_corrupt", details={"phase": "probe", "reason": "invalid_probe_document"}) from exc
    if len(streams) != 1:
        raise MediaIntegrityError("media_unsupported", details={"reason": "expected_one_audio_stream", "audio_streams": len(streams)})
    if not set(formats) & {"flac", "mov", "mp4", "m4a", "mp3", "wav"}:
        raise MediaIntegrityError("media_unsupported", details={"reason": "container", "formats": formats})
    stream = streams[0]
    try:
        rate, channels = int(stream["sample_rate"]), int(stream["channels"])
        if not stream.get("codec_name"):
            raise ValueError("codec is unavailable")
        if not 1 <= rate <= 768000 or not 1 <= channels <= 32:
            raise ValueError("audio parameters outside supported bounds")
    except (KeyError, TypeError, ValueError) as exc:
        raise MediaIntegrityError("media_unsupported", details={"reason": "audio_parameters"}) from exc
    return {**stream, "sample_rate": rate, "channels": channels}, "flac" if "flac" in formats else "wav" if "wav" in formats else "mp3" if "mp3" in formats else "m4a"


def _flac_streaminfo(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        header = handle.read(42)
    if len(header) < 42:
        raise MediaIntegrityError("media_corrupt", details={"phase": "flac_streaminfo", "reason": "truncated_header"})
    if header[:4] != b"fLaC":
        raise MediaIntegrityError("media_unsupported", details={"reason": "non_native_flac_header"})
    if header[4] & 127 != 0 or int.from_bytes(header[5:8], "big") != 34:
        raise MediaIntegrityError("media_corrupt", details={"phase": "flac_streaminfo", "reason": "invalid_streaminfo"})
    info = header[8:42]
    value = int.from_bytes(info[10:18], "big")
    rate, channels, bits = value >> 44, ((value >> 41) & 7) + 1, ((value >> 36) & 31) + 1
    samples = value & ((1 << 36) - 1)
    if not rate or bits not in _PCM or int.from_bytes(info[:2], "big") < 16:
        raise MediaIntegrityError("media_unsupported", details={"reason": "flac_sample_format", "bits_per_sample": bits, "sample_rate": rate})
    return {"sample_rate": rate, "channels": channels, "bits_per_sample": bits,
            "total_samples_per_channel": samples, "pcm_md5": info[18:34].hex()}


def _decode(path: Path, ffmpeg: str, stream: dict[str, Any], container: str,
            deadline: float, max_decoded_bytes: int, max_duration_seconds: float) -> dict[str, Any]:
    flac = _flac_streaminfo(path) if container == "flac" else None
    if flac is not None and (stream["codec_name"] != "flac" or flac["sample_rate"] != stream["sample_rate"] or flac["channels"] != stream["channels"]):
        raise MediaIntegrityError("media_header_mismatch", details={"reason": "flac_probe_parameters_disagree"})
    bits = flac["bits_per_sample"] if flac is not None else 32
    codec, raw_format = _PCM[bits]
    bytes_per_frame = stream["channels"] * (bits // 8)
    pcm_md5, pcm_sha256 = hashlib.md5(usedforsecurity=False), hashlib.sha256()
    byte_count = 0

    def consume(block: bytes) -> None:
        nonlocal byte_count
        byte_count += len(block)
        if byte_count > max_decoded_bytes:
            raise MediaIntegrityError("media_resource_limit", details={"reason": "decoded_bytes", "limit": max_decoded_bytes})
        if byte_count / bytes_per_frame / stream["sample_rate"] > max_duration_seconds:
            raise MediaIntegrityError("media_resource_limit", details={"reason": "decoded_duration", "limit": max_duration_seconds})
        pcm_md5.update(block)
        pcm_sha256.update(block)

    code, error = _run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
                        "-err_detect", "explode", "-threads", "1", "-protocol_whitelist", "file,pipe",
                        "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn",
                        "-ar", str(stream["sample_rate"]), "-ac", str(stream["channels"]),
                        "-c:a", codec, "-f", raw_format, "pipe:1"], deadline, consume)
    if code or error:
        unsupported = any(marker in error.lower() for marker in (
            "unsupported codec", "unknown decoder", "decoder not found", "not implemented",
            "unsupported bit depth", "unsupported sample format"))
        raise MediaIntegrityError("media_unsupported" if unsupported else "media_corrupt",
                                  details={"phase": "full_decode", "returncode": code, "decoder_message": error})
    if not byte_count or byte_count % bytes_per_frame:
        raise MediaIntegrityError("media_corrupt", details={"phase": "full_decode", "reason": "empty_or_partial_pcm_frame", "pcm_bytes": byte_count})
    samples = byte_count // bytes_per_frame
    details: dict[str, Any] = {"container": container, "codec": stream["codec_name"],
        "selected_audio_stream": 0, "sample_rate": stream["sample_rate"], "channels": stream["channels"],
        "decoded_pcm_bits_per_sample": bits, "decoded_pcm_bytes": byte_count,
        "decoded_samples_per_channel": samples, "decoded_duration_seconds": samples / stream["sample_rate"],
        "decoded_pcm_md5": pcm_md5.hexdigest(), "decoded_pcm_sha256": pcm_sha256.hexdigest(),
        "decoder_reported_errors": False, "upstream_completeness_verified": False}
    if flac is not None:
        details["flac_streaminfo"] = flac
        details["flac_sample_count_available"] = bool(flac["total_samples_per_channel"])
        details["flac_pcm_md5_available"] = flac["pcm_md5"] != "0" * 32
        details["flac_sample_count_verified"] = details["flac_sample_count_available"] and samples == flac["total_samples_per_channel"]
        details["flac_pcm_md5_verified"] = details["flac_pcm_md5_available"] and pcm_md5.hexdigest() == flac["pcm_md5"]
        if flac["total_samples_per_channel"] and samples != flac["total_samples_per_channel"]:
            raise MediaIntegrityError("media_header_mismatch", details={**details, "reason": "flac_sample_count_mismatch"})
        if details["flac_pcm_md5_available"] and not details["flac_pcm_md5_verified"]:
            raise MediaIntegrityError("media_checksum_mismatch", details={**details, "reason": "flac_pcm_md5_mismatch"})
        details["verification_scope"] = "flac_full_decode_and_declared_sample_count_and_available_pcm_md5"
        details["limitations"] = ["Validates this file against its own FLAC metadata; does not prove that the upstream recording was complete."]
        if not details["flac_sample_count_available"]:
            details["limitations"].append("STREAMINFO has no declared sample count.")
        if not details["flac_pcm_md5_available"]:
            details["limitations"].append("STREAMINFO has no PCM MD5; valid replacement PCM cannot be excluded by a source checksum.")
    else:
        details["verification_scope"] = "selected_audio_stream_full_decode"
        details["limitations"] = ["Detects reported demux/decode errors; clean truncation or missing upstream audio may remain undetectable without an independent source checksum or sample count."]
        if container == "wav":
            try:
                with wave.open(str(path), "rb") as handle:
                    declared = handle.getnframes()
                details["wav_declared_samples_per_channel"] = declared
                if samples != declared:
                    raise MediaIntegrityError("media_header_mismatch", details={**details, "reason": "wav_sample_count_mismatch"})
                details["verification_scope"] = "wav_full_decode_and_declared_sample_count"
            except (wave.Error, EOFError):
                details["limitations"].append("This WAV encoding has no sample count supported by Python's wave reader.")
    return details


def _write_cache(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".integrity-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(document, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_media(path: Path | str, expected_fingerprint: dict[str, Any], cache_dir: Path | str, *,
                   timeout_seconds: float = 120, max_file_bytes: int = 512 * 1024 * 1024,
                   max_duration_seconds: float = 3600, max_decoded_bytes: int = 2 * 1024 * 1024 * 1024,
                   ffmpeg: str | Path = "ffmpeg", ffprobe: str | Path = "ffprobe") -> dict[str, Any]:
    """Return fingerprint-bound evidence, or raise a typed, non-mutating error.

    Expected fingerprints use the queue's exact sha256/size/mtime_ns fields.
    Cache entries are content/checker-version bound; both cache hits and fresh
    checks re-hash the media before and after use. Only deterministic content
    failures are cached; timeouts, resource bounds and unavailable tools are not.
    """
    if (not isinstance(expected_fingerprint, dict) or set(expected_fingerprint) != {"sha256", "size", "mtime_ns"}
            or not isinstance(expected_fingerprint.get("sha256"), str) or len(expected_fingerprint["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in expected_fingerprint["sha256"])
            or type(expected_fingerprint.get("size")) is not int or expected_fingerprint["size"] < 0
            or type(expected_fingerprint.get("mtime_ns")) is not int):
        raise MediaIntegrityError("media_fingerprint_invalid")
    for value in (timeout_seconds, max_duration_seconds, max_file_bytes, max_decoded_bytes):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("integrity limits must be finite positive numbers")
    started = time.monotonic()
    deadline = started + timeout_seconds
    media = Path(path).absolute()
    expected = dict(expected_fingerprint)
    _check_expected(_fingerprint(media, deadline, max_file_bytes), expected)
    cache = Path(cache_dir) / CHECKER_VERSION / (expected["sha256"] + ".json")
    cache_hit = False
    try:
        try:
            if cache.stat().st_size > 128 * 1024:
                raise ValueError("oversized cache")
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if not isinstance(cached, dict) or cached.get("checker_version") != CHECKER_VERSION or cached.get("source_sha256") != expected["sha256"]:
                raise ValueError("cache identity mismatch")
            if cached.get("status") == "failed" and isinstance(cached.get("error"), dict) and cached["error"].get("code") in _CACHEABLE_ERRORS:
                failure = cached["error"]
                cache_hit = True
                raise MediaIntegrityError(failure["code"], details={**failure.get("details", {}), "cache_hit": True})
            if cached.get("status") != "passed" or not isinstance(cached.get("evidence"), dict):
                raise ValueError("invalid cache status")
            evidence = cached["evidence"]
            if (not evidence.get("decoded_samples_per_channel") or not evidence.get("verification_scope")
                    or not isinstance(evidence.get("decoded_pcm_bytes"), int)
                    or not isinstance(evidence.get("decoded_duration_seconds"), (int, float))
                    or not math.isfinite(evidence["decoded_duration_seconds"])):
                raise ValueError("incomplete cache evidence")
            if evidence.get("decoded_pcm_bytes", float("inf")) > max_decoded_bytes or evidence.get("decoded_duration_seconds", float("inf")) > max_duration_seconds:
                raise MediaIntegrityError("media_resource_limit", details={"reason": "cached_decode_exceeds_current_limits"})
            cache_hit = True
        except (OSError, ValueError, TypeError, KeyError):
            stream, container = _probe(media, _tool(ffprobe), deadline)
            evidence = _decode(media, _tool(ffmpeg), stream, container, deadline, max_decoded_bytes, max_duration_seconds)
            evidence["validated_at"] = time.time()
        _check_expected(_fingerprint(media, deadline, max_file_bytes), expected)
    except (MediaIntegrityError, OSError) as original:
        error = original if isinstance(original, MediaIntegrityError) else MediaIntegrityError(
            "media_io_error", retryable=True, details={"exception": type(original).__name__})
        # Even a failed decode must not associate a result with bytes that
        # changed while the process was reading them.
        if error.code != "media_integrity_timeout":
            _check_expected(_fingerprint(media, deadline, max_file_bytes), expected)
        error.details["checker_version"] = CHECKER_VERSION
        error.details["source_fingerprint"] = expected
        if error.code in _CACHEABLE_ERRORS and not cache_hit:
            try:
                _write_cache(cache, {"checker_version": CHECKER_VERSION, "source_sha256": expected["sha256"], "status": "failed", "error": error.as_dict(), "checked_at": time.time()})
            except OSError:
                error.details["cache_write_failed"] = True
        if error is original:
            raise
        raise error from original
    if not cache_hit:
        try:
            _write_cache(cache, {"checker_version": CHECKER_VERSION, "source_sha256": expected["sha256"], "status": "passed", "evidence": evidence, "checked_at": time.time()})
        except OSError:
            evidence["cache_write_failed"] = True
    return {**copy.deepcopy(evidence), "passed": True, "checker_version": CHECKER_VERSION,
            "source_fingerprint": expected, "cache_hit": cache_hit,
            "elapsed_seconds": round(time.monotonic() - started, 6)}
