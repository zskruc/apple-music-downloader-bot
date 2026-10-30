"""Safe, machine-readable lyric faults and bounded recovery policy.

Exception messages are evidence for classification, never notification text:
provider URLs can contain credentials and lyric bodies.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import random
import re
import socket
import ssl
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SOURCE_POLICY_REVISION = "structured-source-v1"


def _identifier(value: Any, fallback: str) -> str:
    value = str(value or "")
    return value if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,99}", value) else fallback


def sanitize_fault(value: dict[str, Any]) -> dict[str, Any]:
    """Persist identifiers only, even when a caller supplies a custom error."""
    result = {"category": _identifier(value.get("category"), "internal"),
              "code": _identifier(value.get("code"), "unclassified_failure"),
              "retryable": value.get("retryable") is True,
              "action": value.get("action") if value.get("action") in
                  {"retry", "recheck", "needs_attention"} else "needs_attention",
              "provider": _identifier(value.get("provider"), ""),
              "error_type": _identifier(value.get("error_type"), "")}
    status = value.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    after = value.get("retry_after_seconds")
    if isinstance(after, (int, float)) and not isinstance(after, bool) and 0 <= after <= 3600:
        result["retry_after_seconds"] = float(after)
    return result


@dataclass(frozen=True, slots=True)
class LyricFault:
    category: str
    code: str
    retryable: bool = False
    action: str = "needs_attention"
    provider: str = ""
    http_status: int | None = None
    retry_after_seconds: float | None = None
    error_type: str = ""

    def __post_init__(self) -> None:
        # Attribute consumers (logs/retry summaries) get the same safe values
        # as serialized consumers; do not rely on every caller using as_dict.
        safe = sanitize_fault(asdict(self))
        for name in ("category", "code", "retryable", "action", "provider", "error_type"):
            object.__setattr__(self, name, safe[name])
        object.__setattr__(self, "http_status", safe.get("http_status"))
        object.__setattr__(self, "retry_after_seconds", safe.get("retry_after_seconds"))

    def as_dict(self) -> dict[str, Any]:
        return sanitize_fault(asdict(self))


class LyricFaultError(RuntimeError):
    def __init__(self, fault: LyricFault):
        super().__init__(fault.code)
        self.lyric_fault = fault


def classify_exception(exc: BaseException, *, provider: str = "", stage: str = "source") -> LyricFault:
    """Only positive transient transport evidence gets automatic retries."""
    kind = type(exc).__name__
    explicit = getattr(exc, "lyric_fault", None)
    if isinstance(explicit, LyricFault):
        return explicit
    # The media validator is optional here; do not import its decoder/runtime.
    if kind == "MediaIntegrityError":
        retry = getattr(exc, "retryable", False) is True
        return LyricFault("media_integrity", str(exc.code)[:100], retry,
                          "retry" if retry else "needs_attention", provider, error_type=kind)
    if hasattr(exc, "code") and hasattr(exc, "retryable"):
        retry = getattr(exc, "retryable", False) is True
        return LyricFault("playback" if stage == "scan" else "internal", str(exc.code)[:100], retry,
                          "retry" if retry else "needs_attention", provider, error_type=kind)
    if isinstance(exc, urllib.error.HTTPError):
        status = int(exc.code)
        if status == 429 or 500 <= status <= 599:
            retry_after = None
            try:
                retry_after = min(3600.0, max(0.0, float(exc.headers.get("Retry-After", ""))))
            except (TypeError, ValueError, AttributeError):
                pass
            return LyricFault("transport", "http_rate_limit" if status == 429 else "http_server_error",
                              True, "retry", provider, status, retry_after, kind)
        if status in {401, 403}:
            return LyricFault("authentication" if status == 401 else "permission",
                              "http_unauthorized" if status == 401 else "http_forbidden",
                              provider=provider, http_status=status, error_type=kind)
        if status in {404, 410}:
            return LyricFault("no_source", "http_source_absent", action="recheck",
                              provider=provider, http_status=status, error_type=kind)
        return LyricFault("response_semantics", "http_request_rejected", provider=provider,
                          http_status=status, error_type=kind)
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        return classify_exception(exc.reason, provider=provider, stage=stage)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return LyricFault("tls_configuration", "certificate_verification_failed", provider=provider, error_type=kind)
    if isinstance(exc, (ssl.SSLEOFError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return LyricFault("transport", "tls_eof" if isinstance(exc, ssl.SSLEOFError) else "connection_reset",
                          True, "retry", provider, error_type=kind)
    if isinstance(exc, socket.gaierror):
        return LyricFault("transport", "dns_resolution_failed", True, "retry", provider, error_type=kind)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        # Exhausting our own aggregate budget is not proof that the network
        # failed; it needs a policy/runtime diagnosis rather than DNS repair.
        budget = "source_discovery_budget_exhausted" in str(exc)
        return LyricFault("resource_budget" if budget else "transport",
                          "source_budget_exhausted" if budget else "timeout", True, "retry",
                          provider, error_type=kind)
    if isinstance(exc, OSError) and exc.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH,
                                                 errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT}:
        return LyricFault("transport", "connection_unavailable", True, "retry", provider, error_type=kind)
    if isinstance(exc, (json.JSONDecodeError, UnicodeError, ET.ParseError)):
        return LyricFault("parse", "invalid_provider_document", provider=provider, error_type=kind)
    text = str(exc).casefold()
    if any(marker in text for marker in ("audio changed", "lyric changed", "lyric sidecar changed")):
        return LyricFault("content_changed", "publication_changed_concurrently", True, "retry",
                          provider, error_type=kind)
    wrapped_http = re.search(r"http(?:\s+error)?\s+(\d{3})\b", text)
    if wrapped_http:
        status = int(wrapped_http.group(1))
        return classify_exception(urllib.error.HTTPError("", status, "", {}, None),
                                  provider=provider, stage=stage)
    # A few providers currently wrap their underlying exception. Match only
    # named transient types/codes, never vague words such as 'unavailable'.
    if "certificate_verify_failed" in text:
        return LyricFault("tls_configuration", "certificate_verification_failed", provider=provider, error_type=kind)
    for marker, code in (("unexpected_eof_while_reading", "tls_eof"),
                         ("ssl eof", "tls_eof"), ("timeouterror", "timeout"),
                         ("gaierror", "dns_resolution_failed"),
                         ("connectionreseterror", "connection_reset")):
        if marker in text:
            return LyricFault("transport", code, True, "retry", provider, error_type=kind)
    if "source_discovery_budget_exhausted" in text:
        return LyricFault("resource_budget", "source_budget_exhausted", True, "retry", provider, error_type=kind)
    if any(marker in text for marker in ("identity", "version_mismatch", "duration_mismatch",
                                         "no_reference_for_fuzzy", "without_independent_reference")):
        return LyricFault("identity", "recording_identity_unverified", provider=provider, error_type=kind)
    if isinstance(exc, ValueError) or any(marker in text for marker in (
            "invalid krc", "no krc content", "non-object", "invalid record", "invalid song list",
            "invalid search result", "jsondecodeerror", "parseerror")):
        return LyricFault("parse", "invalid_provider_document", provider=provider, error_type=kind)
    if any(marker in text for marker in ("unrelated results", "api error", "http 4", "http 3")):
        return LyricFault("response_semantics", "provider_response_rejected", provider=provider, error_type=kind)
    return LyricFault("internal", "unclassified_" + stage + "_failure", provider=provider, error_type=kind)


def choose_fault(faults: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, Any]:
    # Permanent failure in one optional provider must not mask a viable retry
    # through a different provider. Keep all faults in payload for diagnosis.
    values = list(faults)
    temporary = [value for value in values if value.get("retryable") is True]
    if temporary:
        return temporary[0]
    permanent = [value for value in values if value.get("action") != "recheck"]
    if permanent:
        return permanent[0]
    return values[0] if values else LyricFault("no_source", "no_trusted_lyric_reference", action="recheck").as_dict()


def retry_delay(attempt: int, *, base: float = 300, maximum: float = 3600,
                retry_after: float | None = None, random_value: float | None = None) -> float:
    """Bounded exponential delay with positive jitter; honor numeric Retry-After."""
    spread = random.random() if random_value is None else min(1.0, max(0.0, random_value))
    delay = min(maximum, base * (2 ** min(20, max(0, attempt - 1))))
    return min(maximum, max(delay * (1 + 0.2 * spread), min(maximum, retry_after or 0)))


def source_context_key(media_path: Path, metadata: dict[str, Any], source_url: str = "",
                       *, policy_revision: str | None = None, source_revision: str | None = None) -> str:
    """A changed reference/policy/source permits one fresh bounded lifecycle.

    This is not a media fingerprint and never establishes recording identity.
    The worker/queue separately retain the immutable full media fingerprint.
    """
    sidecars = []
    for suffix in (".ttml", ".lrc", ".elrc", ".txt"):
        path = media_path.with_suffix(suffix)
        try:
            stat = path.stat()
            # Oversized/unreadable references still change the context but
            # cannot force an unbounded file read during queue reconciliation.
            if stat.st_size <= 4 * 1024 * 1024:
                with path.open("rb") as handle:
                    raw = handle.read(4 * 1024 * 1024 + 1)
                digest = hashlib.sha256(raw).hexdigest() if len(raw) <= 4 * 1024 * 1024 else "oversized"
            else:
                digest = "oversized"
            sidecars.append((suffix, stat.st_size, stat.st_mtime_ns, digest))
        except OSError:
            sidecars.append((suffix, "absent"))
    value = {"policy": policy_revision or os.environ.get("WORD_LYRICS_SOURCE_POLICY_REVISION", SOURCE_POLICY_REVISION),
             "source_revision": source_revision if source_revision is not None else os.environ.get("WORD_LYRICS_SOURCE_REVISION", ""),
             "source_url": source_url,
             "metadata": {key: metadata.get(key) for key in ("title", "artist", "album", "duration", "song_id", "isrc")},
             "sidecars": sidecars}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
