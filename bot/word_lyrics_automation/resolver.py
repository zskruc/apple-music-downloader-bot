from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import statistics
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from .fingerprint import media_fingerprint
from .faults import LyricFault, classify_exception, choose_fault
from .io import atomic_write_json
from .instrumental import (INSTRUMENTAL_DISPLAY, confirm_instrumental,
                           confirm_catalog_instrumental, exact_netease_record,
                           is_instrumental_marker, netease_instrumental)
from .identity import artist_matches, artist_search_names, title_matches, title_search_names
from .matching import match_exact_lines, retime_exact_lines_to_references
from .providers.amll import AMLLClient, AMLLLyric
from .providers.apple_identity import AppleArtistIdentity
from .providers.kugou import (
    KugouLyricCandidate,
    fetch_krc,
    parse_krc,
    search as kugou_search,
)
from .providers.netease import (NeteaseSong, fetch_yrc, fetch_lyric_record,
                               parse_yrc, search as netease_search)
from .providers.musiksammler import get_record as musiksammler_get_record
from .providers.lrclib import get_record as lrclib_get_record
from .providers.musicbrainz import get_isrc as musicbrainz_get_isrc
from .text import is_lyric_metadata_line, normalize_text
from .nonlexical import validate_text_coverage
from .reference_fusion import fuse_reference_words
from .ttml import parse_line_references, parse_word_ttml, render_word_ttml


_SEARCH_PUNCTUATION_RE = re.compile(r"[()（）\[\]【】{}《》]")
_SOURCE_OBSERVATION_LIMIT = 128
_SOURCE_FUSION_REASONS = {
    "native timed reference is required": "native_reference_missing",
    "native reference timing is invalid": "native_reference_timing_invalid",
    "provider parallel voice identity is missing": "provider_parallel_voice_identity_missing",
    "insufficient independent reference anchors": "insufficient_reference_anchors",
    "source token timing is invalid": "source_token_timing_invalid",
    "provider has untimed lexical text": "untimed_lexical_text",
    "provider lexical coverage is incomplete": "incomplete_lexical_coverage",
    "provider text has an insertion, omission or phrase change": "lexical_text_changed",
    "provider text agreement is insufficient": "insufficient_text_agreement",
    "multiple substitutions in one native line": "multiple_line_substitutions",
    "provider word clock differs from the native recording": "line_clock_mismatch",
    "provider median word start differs from the native recording": "median_clock_mismatch",
}


def _search_text(value: object) -> str:
    """Remove grouping punctuation that NetEase misreads as search syntax."""

    return " ".join(_SEARCH_PUNCTUATION_RE.sub(" ", str(value or "")).split())


@dataclass(frozen=True, slots=True)
class Candidate:
    provider: str
    provider_id: str
    ttml: str
    exact_identity: bool
    identity_reason: str
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Publication:
    sidecar_path: str
    backup_path: str
    candidate_sha256: str
    provider: str
    provider_id: str
    metrics: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Resolution:
    existing: dict[str, Any] | None = None
    publication: Publication | None = None
    alignment_payload: dict[str, Any] | None = None
    reason: str = ""
    retryable_error: str = ""
    not_required: dict[str, Any] | None = None
    faults: tuple[dict[str, Any], ...] = ()


class CandidateRejected(RuntimeError):
    def __init__(self, reason: str, *, metrics: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.metrics = metrics or {}


def _rollback_sidecar(sidecar_path: str, backup_path: str, candidate_sha256: str) -> None:
    """Undo only this publication; a newer sidecar must win."""
    target = Path(sidecar_path)

    def still_current() -> bool:
        try:
            return hashlib.sha256(target.read_bytes()).hexdigest() == candidate_sha256
        except FileNotFoundError:
            return False

    if not still_current():
        return
    if not backup_path:
        target.unlink(missing_ok=True)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.rollback.", suffix=".tmp"
    )
    os.close(descriptor)
    try:
        shutil.copy2(backup_path, temporary_name)
        # Check again after preparing the backup, before replacing the current
        # sidecar, so another writer's completed publication is not undone.
        if still_current():
            os.replace(temporary_name, target)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _first(values: Iterable[str]) -> str:
    return next((value for value in values if value), "")


class LegacyGateway:
    """Narrow adapter to the already-deployed, audited lyric parser/writer."""

    def __init__(self, state_dir: Path) -> None:
        import word_lyrics as legacy

        self.legacy = legacy
        self.state_dir = state_dir
        self.service = object.__new__(legacy.WordLyricsService)
        self.service.state_dir = state_dir
        self.service.audit = legacy.AuditStore(state_dir)

    def validate_media(self, media_path: Path, *, expected_fingerprint):
        if os.environ.get("WORD_LYRICS_MEDIA_INTEGRITY_ENABLED", "true").lower() in {"0","false","no"}:
            return {"checked": False, "reason": "explicitly_disabled"}
        from .media_integrity import validate_media
        return validate_media(media_path, expected_fingerprint,
            self.state_dir / "automation" / "media-integrity",
            timeout_seconds=float(os.environ.get("WORD_LYRICS_MEDIA_CHECK_TIMEOUT_SECONDS", "120")))

    def request_player_scan(self):
        if os.environ.get("WORD_LYRICS_PLAYER_VERIFY_ENABLED", "true").lower() in {"0","false","no"}:
            from youtube_ingest import trigger_navidrome_scan
            return trigger_navidrome_scan()
        from .player_verification import request_from_environment
        return request_from_environment()("startScan").get("status")=="ok"

    def verify_player(self, job, result):
        if os.environ.get("WORD_LYRICS_PLAYER_VERIFY_ENABLED", "true").lower() in {"0","false","no"}:
            return {"checked": False, "reason": "explicitly_disabled"}
        from .player_verification import verify_publication
        return verify_publication(Path(job.media_path), job.metadata, result, job.fingerprint_data,
            evidence_dir=self.state_dir / "automation" / "player-verification")

    def inspect(self, media_path: Path, metadata: dict[str, Any]) -> Any:
        seed = self.legacy.Track(
            path=media_path,
            title=str(metadata.get("title") or ""),
            artist=str(metadata.get("artist") or ""),
            album=str(metadata.get("album") or ""),
            duration=float(metadata.get("duration") or 0),
            isrc=str(metadata.get("isrc") or ""),
            song_id=str(metadata.get("song_id") or ""),
            navidrome_id=str(metadata.get("navidrome_id") or ""),
        )
        return self.legacy.inspect_track(media_path, seed)

    def existing_word(self, media_path: Path, duration: float = 0.0) -> dict[str, Any] | None:
        existing = self.service.existing_word_sidecar(media_path, duration=duration)
        if existing is None:
            return None
        path, _ = existing
        # Bind the reused publication to the exact bytes actually validated,
        # rather than hashing a later replacement after the parser returned.
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig", errors="strict")
            document = (self.legacy.parse_ttml(text) if path.suffix.casefold() == ".ttml"
                        else self.legacy.parse_elrc(text))
            self.legacy.validate_word_timing(text, document, duration=duration)
        except (OSError, UnicodeError, ValueError, ET.ParseError):
            return None
        return {
            "reason": "valid_word_sidecar_already_exists",
            "sidecar_path": str(path),
            "candidate_sha256": hashlib.sha256(raw).hexdigest(),
            "timed_words": document.timed_spans,
            "timed_lines": len(document.lines),
        }

    def references(self, media_path: Path) -> list[tuple[Path, Any]]:
        return [(path, document) for path, document in self.service.reference_documents(media_path)
                if not (document.lines and all(is_instrumental_marker(line.text) for line in document.lines))]

    def instrumental_result(self, track: Any, expected_fingerprint: dict[str, Any],
                            artist_names: list[str], *,
                            lrclib_fetch_function: Any = lrclib_get_record,
                            musicbrainz_fetch_function: Any = musicbrainz_get_isrc,
                            catalog_fetch_function: Any = musiksammler_get_record,
                            netease_records: list[tuple[NeteaseSong, dict[str, Any]]] | None = None,
                            check_budget: Any = None) -> dict[str, Any] | None:
        # Never let an online flag replace local sung text, including embedded
        # plain lyrics that are not part of the timed-reference reader.
        if self.references(track.path) or self.existing_word(track.path):
            return None
        from mutagen import File
        audio = File(track.path)
        for key, value in (audio.tags or {}).items() if audio is not None else []:
            if "lyr" in key.casefold() or key.casefold().startswith(("uslt", "sylt")):
                values = value if isinstance(value, list) else [getattr(value, "text", value)]
                if any(str(text).strip() and not is_instrumental_marker(str(text)) for text in values):
                    return None
        for suffix in (".txt", ".srt", ".yaml", ".yml"):
            path = track.path.with_suffix(suffix)
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                if text and not is_instrumental_marker(text):
                    return None
        evidence = None
        failures = []
        # The provider's explicit flag is retained, rather than discarded when
        # a track has no YRC. An independent release must explicitly agree.
        for song, lyric_record in netease_records or []:
            if not exact_netease_record(track, artist_names, song) or not netease_instrumental(lyric_record):
                continue
            primary_artist = re.split(r"\s*(?:/|&|、|,|，)\s*", song.artist)[0]
            try:
                if check_budget:
                    check_budget()
                catalog = catalog_fetch_function(title=track.title, album=track.album, artist=primary_artist)
                evidence = confirm_catalog_instrumental(track, artist_names, song, lyric_record, catalog or {})
                if evidence:
                    break
            except Exception as exc:
                failures.append("musiksammler:" + type(exc).__name__ + ":" + str(exc)[:160])
        isrc_record = None
        if evidence is None:
            for artist in dict.fromkeys([track.artist, *artist_names]):
                try:
                    if check_budget:
                        check_budget()
                    record = lrclib_fetch_function(title=track.title, artist=artist,
                                                  album=track.album, duration=track.duration)
                    if not record or record.get("instrumental") is not True:
                        continue
                    if isrc_record is None:
                        isrc_record = musicbrainz_fetch_function(track.isrc) or {}
                    evidence = confirm_instrumental(track, artist_names, record, isrc_record)
                    if evidence:
                        break
                except Exception as exc:
                    # An outage in one database must not abort other verified
                    # names or the independent alternative classification path.
                    failures.append("lrclib_musicbrainz:" + type(exc).__name__ + ":" + str(exc)[:160])
        if evidence is None:
            if failures:
                raise RuntimeError("; ".join(dict.fromkeys(failures)))
            return None
        current = media_fingerprint(track.path)
        if current != expected_fingerprint:
            raise CandidateRejected("audio_changed_since_enqueue")
        # Re-read before writing, so a concurrently added native lyric wins.
        if self.references(track.path) or self.existing_word(track.path):
            return None
        marker = "[00:00.00]" + INSTRUMENTAL_DISPLAY + "\n"
        digest = hashlib.sha256(marker.encode()).hexdigest()
        evidence_path = self.state_dir / "automation" / "source-evidence" / ("instrumental-" + current["sha256"] + ".json")
        metrics = {"classification": "instrumental_or_wordless", "timed_words": 0,
                   "source_evidence_path": str(evidence_path), "independent_sources": 2}
        atomic_write_json(evidence_path, {"source_fingerprint": current, "decision": evidence,
            "candidate_sha256": digest, "track": {key: getattr(track, key) for key in
                ("title", "artist", "album", "duration", "song_id", "isrc")},
            "verified_artist_names": artist_names})
        sidecar_path, backup_path = self.service.write_text_sidecar(track.path, marker, suffix=".lrc")
        try:
            if media_fingerprint(track.path) != current:
                raise CandidateRejected("audio_changed_while_marking_instrumental")
        except Exception:
            _rollback_sidecar(sidecar_path, backup_path, digest)
            raise
        result = {"lyric_status": "not_required", "reason": "confirmed_instrumental",
                  "provider": evidence.get("provider", "lrclib_musicbrainz"), "sidecar_path": sidecar_path,
                  "backup_path": backup_path, "candidate_sha256": digest, "metrics": metrics}
        self.service.audit.record(self.legacy.Result(media_path=str(track.path), song_id=track.song_id,
            status="not_required", reason="confirmed_instrumental", identity_confidence="two_source_exact_recording",
            sidecar_path=sidecar_path, backup_path=backup_path, reference_path="",
            candidate_sha256=digest, metrics=metrics))
        return result

    def netease_reference(self, track: Any, song: NeteaseSong,
                         record: dict[str, Any]) -> dict[str, Any] | None:
        if record.get("pureMusic") is True or record.get("nolyric") is True:
            return None
        raw = (record.get("lrc") or {}).get("lyric") or ""
        document = self.legacy.parse_lrc(raw)
        lines = [line for line in document.lines
                 if not is_instrumental_marker(line.text)
                 and not is_lyric_metadata_line(line.text, title=song.title, artist=song.artist,
                                               begin=line.begin)]
        if (len(lines) < 4 or len(normalize_text("".join(line.text for line in lines))) < 40
                or any(line.begin < 0 or line.begin > track.duration for line in lines)
                or any(right.begin <= left.begin for left, right in zip(lines, lines[1:]))):
            return None
        return {
            "reference_path": "netease:" + song.song_id + ":exact-recording-lrc",
            "reference_text": "".join(line.text for line in lines),
            "reference_lines": [{"text": line.text, "begin": line.begin, "end": line.end}
                                for line in lines],
            "reference_provider": "netease_exact_lrc", "reference_provider_id": song.song_id,
            "reference_identity": asdict(song),
        }

    def localized_artist_identity(self, track: Any) -> dict[str, Any]:
        def client() -> Any:
            return self.legacy.AppleMusicClient(
                wrapper_url=os.environ.get("WORD_LYRICS_APPLE_WRAPPER_URL", "http://wrapper"),
                storefronts=("us", "jp", "cn"), timeout=10, request_interval=0.2,
            )
        return AppleArtistIdentity(
            self.state_dir / "automation" / "artist-identities", client
        ).resolve(track)

    @staticmethod
    def _reference_rows(path: Path, document: Any) -> list[dict[str, Any]]:
        rows = [{"text": line.text, "begin": line.begin, "end": line.end}
                for line in document.lines]
        # The legacy reader exposes text/time only. Read native TTML directly
        # so neither enqueue nor the final publication check erases voices.
        # Keep its inferred/missing ends for old single-voice TTML references.
        if path.suffix.casefold() != ".ttml":
            return rows
        try:
            root = ET.fromstring(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError, ET.ParseError) as exc:
            raise CandidateRejected("native_reference_structure_unreadable") from exc
        from .ttml import ITUNES_NS, TTM_NS, _native_metadata, local_name
        metadata_text = _native_metadata(root)
        structured: list[dict[str, Any]] = []

        def visit(element: ET.Element, inherited_agent: str | None = None) -> None:
            agent = element.get(f"{{{TTM_NS}}}agent", inherited_agent)
            if local_name(str(element.tag)) == "p":
                text = "".join(element.itertext()).strip()
                if normalize_text(text):
                    structured.append({"text": text, "key": element.get(f"{{{ITUNES_NS}}}key"),
                                       "agent": agent, "metadata": metadata_text})
                return
            for child in element:
                visit(child, agent)

        visit(root)
        if (len(structured) != len(rows)
                or [line["text"] for line in structured] != [line["text"] for line in rows]):
            raise CandidateRejected("native_reference_structure_mismatch")
        return [{**row, **structure} for row, structure in zip(rows, structured, strict=True)]

    def alignment_payload(self, track: Any, fingerprint_data: dict[str, Any]) -> dict[str, Any]:
        references = self.references(track.path)
        # Preserve the most complete text, then prefer explicit line ends.
        # A duplicate LRC drops the ends available in the native line TTML.
        references.sort(key=lambda item: (
            len(item[1].normalized_text),
            sum(line.end is not None and line.end > line.begin for line in item[1].lines),
        ), reverse=True)
        reference_path = ""
        reference_lines: list[dict[str, Any]] = []
        reference_text = ""
        if references:
            path, document = references[0]
            reference_path = str(path)
            reference_text = "".join(line.text for line in document.lines)
            reference_lines = self._reference_rows(path, document)
        return {
            "track": {
                "title": track.title,
                "artist": track.artist,
                "album": track.album,
                "duration": track.duration,
                "song_id": track.song_id,
                "isrc": track.isrc,
            },
            "reference_path": reference_path,
            "reference_text": reference_text,
            "reference_lines": reference_lines,
            "source_fingerprint": fingerprint_data,
        }

    def _strict_validation(
        self,
        track: Any,
        candidate: Candidate,
        *,
        mac_report: dict[str, Any] | None = None,
        alignment_payload: dict[str, Any] | None = None,
    ) -> tuple[Any, dict[str, Any], str]:
        try:
            document = self.legacy.parse_ttml(candidate.ttml)
        except Exception as exc:
            raise CandidateRejected(f"invalid_ttml:{type(exc).__name__}") from exc
        try:
            semantic_lines = parse_word_ttml(candidate.ttml)
        except Exception as exc:
            raise CandidateRejected(
                f"invalid_nested_ttml_timing:{type(exc).__name__}"
            ) from exc
        semantic_cues = sum(len(line.tokens) for line in semantic_lines)
        if not semantic_lines or semantic_cues != document.timed_spans:
            raise CandidateRejected("nested_ttml_cue_count_mismatch")
        try:
            validate_text_coverage(semantic_lines)
        except ValueError as exc:
            raise CandidateRejected(str(exc)) from exc
        for line in semantic_lines:
            previous_begin = line.begin
            previous_end = line.begin
            for token in line.tokens:
                if (
                    not all(math.isfinite(value) for value in (token.begin, token.end))
                    or token.begin < line.begin - 0.001
                    or token.end > line.end + 0.001
                    or token.end <= token.begin
                    or token.begin < previous_begin - 0.001
                    or token.end < previous_end - 0.001
                ):
                    raise CandidateRejected("nested_ttml_cue_timing_invalid")
                previous_begin = token.begin
                previous_end = token.end
        if not document.is_word_synced or document.timed_spans < 2:
            raise CandidateRejected("candidate_has_no_word_timing")
        if document.max_end is None or document.max_end <= 0:
            raise CandidateRejected("candidate_has_no_valid_end_time")
        if track.duration > 0 and document.max_end > track.duration + 0.5:
            raise CandidateRejected("candidate_exceeds_audio_duration")
        references = self.references(track.path)
        fusion_metrics = {}
        if candidate.provider == 'kugou_reference_fusion':
            # Rebuild from the retained source and the CURRENT native sidecar.
            # A self-reported similarity score is not publication evidence.
            evidence = candidate.details.get('fusion_evidence') or {}
            identity = evidence.get('identity') or {}
            try:
                source_duration = float(identity.get('duration_ms', 0)) / 1000
            except (ValueError, TypeError) as exc:
                raise CandidateRejected('fusion_source_identity_mismatch') from exc
            if (not title_matches(track.title, str(identity.get('title') or ''))
                    or not artist_matches(track.artist, str(identity.get('artist') or ''))
                    or '官方' not in str(identity.get('product_from') or '')
                    or not math.isfinite(source_duration)
                    or str(identity.get('lyric_id')) != candidate.provider_id
                    or abs(track.duration - source_duration) > 3):
                raise CandidateRejected('fusion_source_identity_mismatch')
            native = self.alignment_payload(track, {})
            try:
                rebuilt, fusion_metrics = fuse_reference_words(native['reference_lines'],
                    parse_krc(evidence['krc'], title=identity['title'], artist=identity['artist']),
                    duration=track.duration, title=track.title, artist=track.artist)
            except (KeyError, ValueError, TypeError, RuntimeError) as exc:
                raise CandidateRejected('fusion_evidence_invalid:' + str(exc)) from exc
            if rebuilt != candidate.ttml:
                raise CandidateRejected('fusion_candidate_does_not_match_evidence')
        same_file_alignment = (
            candidate.exact_identity
            and candidate.identity_reason == "acoustic_same_file"
            and mac_report is not None
            and alignment_payload is not None
        )
        if same_file_alignment:
            self._validate_alignment_reference(semantic_lines, alignment_payload, track.duration)
        validation = self.legacy.validate_candidate(
            document,
            # Cross-recording start-time similarity is not an acoustic quality
            # check. A same-file alignment may refine the old line starts while
            # preserving every reference line and its local acoustic window.
            [] if same_file_alignment else references,
            exact_identity=candidate.exact_identity,
            identity_confidence=candidate.identity_reason,
            duration=track.duration,
        )
        if not validation.accepted:
            raise CandidateRejected(validation.reason, metrics=validation.metrics)
        metrics = dict(validation.metrics)
        metrics.update(fusion_metrics)
        if same_file_alignment:
            metrics["validation_method"] = "same_file_reference_alignment"
            metrics["reference_comparisons"] = [
                {"path": str(path), **self.legacy.comparison_metrics(document, reference)}
                for path, reference in references
            ]
        if references:
            exact_reference = next(
                (
                    (path, reference)
                    for path, reference in references
                    if reference.normalized_text == document.normalized_text
                ),
                None,
            )
            if exact_reference is None:
                raise CandidateRejected("candidate_text_not_exactly_equal_to_reference")
            reference_path = str(exact_reference[0])
            if exact_reference[0].suffix.casefold() == ".ttml":
                current_rows = self._reference_rows(*exact_reference)
                self._validate_alignment_reference(semantic_lines, {
                    "reference_text": "".join(row["text"] for row in current_rows),
                    "reference_lines": current_rows,
                }, track.duration)
            metrics["reference_text_similarity"] = 1.0
        else:
            reference_path = ""
            if not candidate.exact_identity and mac_report is None:
                raise CandidateRejected("external_source_without_independent_reference")
        if same_file_alignment and not alignment_payload.get("reference_provider"):
            original_path = str(alignment_payload.get("reference_path") or "")
            if original_path:
                # Recheck only the original local authority, not a newly
                # discovered translation or an unrelated longer sidecar.
                current = next((reference for path, reference in references
                                if str(path) == original_path), None)
                if current is None:
                    raise CandidateRejected("alignment_reference_missing_since_enqueue")
                current_reference = {
                    "reference_text": "".join(line.text for line in current.lines),
                    "reference_lines": self._reference_rows(Path(original_path), current),
                }
                self._validate_alignment_reference(semantic_lines, current_reference, track.duration)
        if mac_report is not None:
            mode = str(mac_report.get("mode") or "")
            if mode == "reference_aligned":
                required = {
                    "reference_text_similarity": 1.0,
                    "matched_reference_character_coverage": 0.70,
                    "raw_transcript_text_similarity": 0.60,
                }
                for key, minimum in required.items():
                    value = float(mac_report.get(key) or 0)
                    if not math.isfinite(value) or not minimum - 1e-9 <= value <= 1.0:
                        raise CandidateRejected(f"mac_quality_gate_failed:{key}")
                unverified = float(mac_report.get("unverified_line_fraction", 1.0))
                if not math.isfinite(unverified) or not 0 <= unverified <= 0.10:
                    raise CandidateRejected("mac_quality_gate_failed:unverified_line_fraction")
            elif mode == "free_transcription":
                raise CandidateRejected(
                    "free_transcription_without_reference_not_publishable"
                )
            else:
                raise CandidateRejected("unknown_mac_candidate_mode")
            metrics.update({f"mac_{key}": value for key, value in mac_report.items()})
        metrics.update(
            {
                "provider": candidate.provider,
                "provider_id": candidate.provider_id,
                "timed_words": document.timed_spans,
                "timed_lines": len(document.lines),
                "audio_duration_seconds": round(float(track.duration or 0), 6),
                "lyrics_end_seconds": round(float(document.max_end or 0), 6),
            }
        )
        return document, metrics, reference_path

    @staticmethod
    def _validate_alignment_reference(lines: list[Any], payload: dict[str, Any], duration: float) -> None:
        from .alignment import _reference_lines

        references = _reference_lines(payload, duration)
        if len(lines) != len(references) or not lines:
            raise CandidateRejected("alignment_reference_line_count_mismatch")
        previous_begins: dict[str, float] = {}
        completed: dict[str, list[tuple[float, float]]] = {}
        for line, reference in zip(lines, references, strict=True):
            if line.text != str(reference["text"]):
                raise CandidateRejected("alignment_reference_line_text_mismatch")
            if reference.get("key") is not None and getattr(line, "key", None) != reference["key"]:
                raise CandidateRejected("alignment_reference_line_key_mismatch")
            if reference.get("agent") is not None and getattr(line, "agent", None) != reference["agent"]:
                raise CandidateRejected("alignment_reference_line_agent_mismatch")
            if (reference.get("metadata") is not None
                    and getattr(line, "metadata", None) != reference["metadata"]):
                raise CandidateRejected("alignment_reference_metadata_mismatch")
            begin, end = reference["begin"], reference["end"]
            padding = max(1.25, min(2.5, (end - begin) * 0.4))
            # Native voices are independent clocks. Within each voice retain
            # both document start order and every non-overlap boundary.
            voice = str(reference.get("agent") or "")
            voice_completed = completed.setdefault(voice, [])
            order_floor = max(
                [previous_begins.get(voice, 0.0)]
                + [output_end for reference_end, output_end in voice_completed if reference_end <= begin]
            )
            if (
                not all(math.isfinite(value) for value in (begin, end, line.begin, line.end))
                or begin < 0 or end <= begin or end > duration + 0.5
                or line.begin < order_floor - 0.001
                or line.begin < begin - padding - 0.001
                or line.end > end + padding + 0.001
            ):
                raise CandidateRejected("alignment_reference_time_window_mismatch")
            previous_begins[voice] = line.begin
            voice_completed.append((end, line.end))

    def publish(
        self,
        track: Any,
        candidate: Candidate,
        *,
        expected_fingerprint: dict[str, Any],
        mac_report: dict[str, Any] | None = None,
        alignment_payload: dict[str, Any] | None = None,
    ) -> Publication:
        self.validate_media(track.path, expected_fingerprint=expected_fingerprint)
        current_fingerprint = media_fingerprint(track.path)
        if current_fingerprint != expected_fingerprint:
            raise CandidateRejected("audio_changed_since_enqueue")
        if mac_report is not None and mac_report.get("source_fingerprint") != current_fingerprint:
            raise CandidateRejected("mac_report_fingerprint_mismatch")
        _, metrics, reference_path = self._strict_validation(
            track,
            candidate,
            mac_report=mac_report,
            alignment_payload=alignment_payload,
        )
        digest = hashlib.sha256(candidate.ttml.encode("utf-8")).hexdigest()
        if candidate.provider == 'kugou_reference_fusion':
            evidence_path = self.state_dir / 'automation/source-evidence' / (digest + '.json')
            atomic_write_json(evidence_path, {'source_fingerprint': current_fingerprint,
                'candidate_sha256': digest, 'reference': self.alignment_payload(track, current_fingerprint),
                'source': candidate.details['fusion_evidence'], 'metrics': metrics})
            metrics['source_evidence_path'] = str(evidence_path)
        sidecar_path, backup_path = self.service.write_sidecar(track.path, candidate.ttml)
        try:
            after_fingerprint = media_fingerprint(track.path)
            if after_fingerprint != current_fingerprint:
                raise CandidateRejected("audio_changed_while_publishing_sidecar")
        except Exception:
            _rollback_sidecar(sidecar_path, backup_path, digest)
            raise
        result = self.legacy.Result(
            media_path=str(track.path),
            song_id=track.song_id,
            status="accepted",
            reason=f"automation_{candidate.provider}_{candidate.identity_reason}",
            identity_confidence=candidate.identity_reason,
            sidecar_path=sidecar_path,
            backup_path=backup_path,
            reference_path=reference_path,
            candidate_sha256=digest,
            metrics=metrics,
        )
        self.service.audit.record(result)
        return Publication(
            sidecar_path=sidecar_path,
            backup_path=backup_path,
            candidate_sha256=digest,
            provider=candidate.provider,
            provider_id=candidate.provider_id,
            metrics=metrics,
        )


class SourceResolutionEngine:
    def __init__(
        self,
        gateway: LegacyGateway,
        *,
        amll_client: AMLLClient | None = None,
        netease_search_function: Any = netease_search,
        netease_fetch_function: Any = fetch_yrc,
        netease_record_function: Any = None,
        kugou_search_function: Any = kugou_search,
        kugou_fetch_function: Any = fetch_krc,
    ) -> None:
        self.gateway = gateway
        self.amll = amll_client or AMLLClient()
        self.netease_search = netease_search_function
        self.netease_fetch = netease_fetch_function
        self.netease_record_fetch = netease_record_function or (
            fetch_lyric_record if netease_fetch_function is fetch_yrc else None)
        self.netease_records: dict[str, tuple[NeteaseSong, dict[str, Any]]] = {}
        self.kugou_search = kugou_search_function
        self.kugou_fetch = kugou_fetch_function
        self.deadline = float("inf")
        self.source_diagnostics: list[dict[str, Any]] = []
        self.source_diagnostic_counts: dict[str, int] = {}

    def observe_source(self, provider: str, event: str, **details: Any) -> None:
        """Keep bounded metadata diagnostics; never pass lyric bodies or keys."""
        def bounded(value: Any) -> Any:
            if isinstance(value, str):
                return value[:200]
            if isinstance(value, dict):
                return {key: bounded(item) for key, item in list(value.items())[:16]}
            if isinstance(value, (tuple, list)):
                return [bounded(item) for item in value[:12]]
            return value

        key = f"{provider}:{event}"
        self.source_diagnostic_counts[key] = self.source_diagnostic_counts.get(key, 0) + 1
        self.source_diagnostics.append({"provider": provider, "event": event, **bounded(details)})
        del self.source_diagnostics[:-_SOURCE_OBSERVATION_LIMIT]

    def source_observations(self) -> list[dict[str, Any]]:
        # Preserve the original NetEase record shape for existing consumers.
        records = [
            {"provider": "netease", "song": {
                 key: value[:200] if isinstance(value, str) else value
                 for key, value in asdict(song).items()},
             "instrumental_flag": netease_instrumental(record),
             "line_text_available": bool((record.get("lrc") or {}).get("lyric")),
             "word_text_available": bool((record.get("yrc") or {}).get("lyric"))}
            for song, record in self.netease_records.values()
        ][:_SOURCE_OBSERVATION_LIMIT - 1]
        room = _SOURCE_OBSERVATION_LIMIT - len(records) - 1
        observations = records + (self.source_diagnostics[-room:] if room else [])
        if self.source_diagnostic_counts:
            observations.append({"provider": "source_diagnostics", "event": "summary",
                "counts": dict(self.source_diagnostic_counts),
                "omitted_events": sum(self.source_diagnostic_counts.values()) - min(room, len(self.source_diagnostics))})
        return observations

    @staticmethod
    def _provider_failure(provider: str, exc: Exception) -> dict[str, Any]:
        fault = classify_exception(exc, provider=provider)
        # Store safe identifiers, not raw URLs, keys or response bodies.
        safe_code = "source_discovery_budget_exhausted" if fault.code == "source_budget_exhausted" else fault.code
        return {"provider": provider, "reason": type(exc).__name__ + ":" + safe_code,
                "fault": fault.as_dict()}

    def check_budget(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("source_discovery_budget_exhausted")

    @staticmethod
    def _amll_identity(track: Any, item: AMLLLyric) -> tuple[bool, str] | None:
        title_ok = any(title_matches(track.title, value) for value in item.music_names)
        artist_ok = any(artist_matches(track.artist, value) for value in item.artist_names)
        apple_exact = bool(track.song_id and track.song_id in item.apple_ids)
        isrc_exact = bool(track.isrc and track.isrc.upper() in item.isrcs)
        if apple_exact:
            return True, "exact_apple_music_id"
        if isrc_exact:
            return True, "exact_isrc"
        if title_ok and artist_ok:
            return False, "strict_metadata"
        return None

    def _amll_candidates(self, track: Any) -> Iterable[Candidate]:
        items: list[AMLLLyric] = []
        if track.song_id:
            self.check_budget()
            item = self.amll.get(apple_id=track.song_id)
            if item is not None:
                items.append(item)
        if track.isrc:
            self.check_budget()
            item = self.amll.get(isrc=track.isrc)
            if item is not None:
                items.append(item)
        for artist in artist_search_names(track.artist):
            self.check_budget()
            items.extend(
                self.amll.search(title=track.title, artist=artist, album=track.album)
            )
        seen: set[str] = set()
        for item in items:
            if not item.lyric_id or item.lyric_id in seen:
                continue
            seen.add(item.lyric_id)
            identity = self._amll_identity(track, item)
            if identity is None:
                continue
            self.check_budget()
            materialized = self.amll.fetch(item)
            if materialized is None or not materialized.lyrics:
                continue
            exact, reason = identity
            yield Candidate(
                provider="amll",
                provider_id=item.lyric_id,
                ttml=materialized.lyrics,
                exact_identity=exact,
                identity_reason=reason,
                details={
                    "filename": materialized.filename,
                    "apple_ids": list(materialized.apple_ids),
                    "ncm_ids": list(materialized.ncm_ids),
                    "isrcs": list(materialized.isrcs),
                },
            )

    @staticmethod
    def _netease_identity(track: Any, song: NeteaseSong) -> tuple[bool, float]:
        duration_delta = abs(float(track.duration or 0) - song.duration_ms / 1000.0)
        accepted = (
            title_matches(track.title, song.title)
            and artist_matches(track.artist, song.artist)
            and float(track.duration or 0) > 0
            and duration_delta <= 0.75
        )
        return accepted, duration_delta

    def _netease_candidates(self, track: Any) -> Iterable[Candidate]:
        references = self.gateway.references(track.path)
        clean_title = _search_text(track.title)
        clean_album = _search_text(track.album)
        raw_queries = []
        for artist in artist_search_names(track.artist):
            clean_artist = _search_text(artist)
            raw_queries.extend([
                f"{clean_album} {clean_title} {clean_artist}" if clean_album else "",
                f"{clean_title} {clean_artist}",
                f"{track.album} {track.title} {artist}" if track.album else "",
                f"{track.title} {artist}",
            ])
        queries: list[str] = []
        for query in raw_queries:
            query = " ".join(query.split())
            if query and query not in queries:
                queries.append(query)
        songs: list[NeteaseSong] = []
        seen_song_ids: set[str] = set()
        for query in queries:
            self.check_budget()
            found = self.netease_search(query, limit=50)
            self.observe_source("netease", "search", query=query, result_count=len(found))
            for song in found:
                if song.song_id and song.song_id not in seen_song_ids:
                    seen_song_ids.add(song.song_id)
                    songs.append(song)
        ranked: list[tuple[float, NeteaseSong]] = []
        for song in songs:
            accepted, duration_delta = self._netease_identity(track, song)
            reasons = []
            if not title_matches(track.title, song.title):
                reasons.append("title_mismatch")
            if not artist_matches(track.artist, song.artist):
                reasons.append("artist_mismatch")
            if float(track.duration or 0) <= 0:
                reasons.append("track_duration_missing")
            elif duration_delta > 0.75:
                reasons.append("duration_mismatch")
            self.observe_source("netease", "identity_accepted" if accepted else "identity_rejected",
                song=asdict(song), reasons=reasons, duration_delta_seconds=round(duration_delta, 6))
            if accepted:
                ranked.append((duration_delta, song))
        self.observe_source("netease", "selection", unique_results=len(songs),
            identity_matches=len(ranked), selected_count=min(5, len(ranked)))
        for duration_delta, song in sorted(ranked, key=lambda item: item[0])[:5]:
            self.check_budget()
            if self.netease_record_fetch is not None:
                if song.song_id not in self.netease_records:
                    self.netease_records[song.song_id] = (song, self.netease_record_fetch(song.song_id))
                record = self.netease_records[song.song_id][1]
                yrc = (record.get("yrc") or {}).get("lyric") or ""
            else:
                yrc = self.netease_fetch(song.song_id)
            source_lines = parse_yrc(yrc)
            if not source_lines:
                self.observe_source("netease", "word_text_unavailable", provider_id=song.song_id,
                    reason="no_parseable_yrc" if yrc else "no_yrc")
                continue
            self.observe_source("netease", "word_text_available", provider_id=song.song_id,
                line_count=len(source_lines), token_count=sum(len(line.tokens) for line in source_lines))
            matched_lines = source_lines
            match_metrics: dict[str, Any] = {}
            exact_line_reference = False
            for path, _ in references:
                if path.suffix.casefold() != ".ttml":
                    continue
                try:
                    raw_reference = path.read_text(encoding="utf-8", errors="replace")
                    line_references = parse_line_references(raw_reference)
                    report = match_exact_lines(line_references, source_lines)
                except Exception as exc:
                    self.observe_source("netease", "fusion_rejected", provider_id=song.song_id,
                        reason="reference_text_or_structure_mismatch", error_type=type(exc).__name__)
                    continue
                start_inlier_fraction = sum(
                    delta <= 0.50 for delta in report.start_deltas
                ) / max(1, len(report.start_deltas))
                end_inlier_fraction = sum(
                    delta <= 0.50 for delta in report.end_deltas
                ) / max(1, len(report.end_deltas))
                # Line endings are deliberately not an acceptance gate here:
                # Apple commonly keeps a lyric row active through the
                # following pause, while YRC ends it at the final syllable.
                # Exact full-text coverage and the line starts identify the
                # same recording; Apple envelopes replace both provider ends.
                if (
                    report.median_start_delta > 0.40
                    or start_inlier_fraction < 0.80
                    or report.maximum_start_delta > 8.0
                    or report.skipped_source_indexes
                ):
                    self.observe_source("netease", "fusion_rejected", provider_id=song.song_id,
                        reason="native_line_clock_mismatch", median_start_delta_seconds=round(report.median_start_delta, 6),
                        maximum_start_delta_seconds=round(report.maximum_start_delta, 6),
                        start_inlier_fraction=round(start_inlier_fraction, 6),
                        skipped_source_lines=len(report.skipped_source_indexes))
                    continue
                try:
                    matched_lines = list(
                        retime_exact_lines_to_references(
                            line_references,
                            report.matched_lines,
                        )
                    )
                except Exception as exc:
                    self.observe_source("netease", "fusion_rejected", provider_id=song.song_id,
                        reason="karaoke_unit_mapping_invalid", error_type=type(exc).__name__)
                    continue
                exact_line_reference = True
                self.observe_source("netease", "fusion_accepted", provider_id=song.song_id,
                    reference_line_count=len(line_references))
                match_metrics = {
                    "median_start_delta_seconds": report.median_start_delta,
                    "maximum_start_delta_seconds": report.maximum_start_delta,
                    "median_end_delta_seconds": report.median_end_delta,
                    "line_start_inlier_fraction": start_inlier_fraction,
                    "line_end_inlier_fraction": end_inlier_fraction,
                    "skipped_source_indexes": list(report.skipped_source_indexes),
                }
                break
            ttml = render_word_ttml(
                matched_lines,
                duration=float(track.duration),
                title=track.title,
                artist=track.artist,
            )
            yield Candidate(
                provider="netease_yrc",
                provider_id=song.song_id,
                ttml=ttml,
                exact_identity=False,
                identity_reason=(
                    "exact_timed_reference_fusion"
                    if exact_line_reference
                    else "strict_metadata_duration"
                ),
                details={
                    "duration_delta_seconds": duration_delta,
                    "song": asdict(song),
                    **match_metrics,
                },
            )

    def _kugou_reference(self, track: Any) -> dict[str, Any] | None:
        duration_ms = round(float(track.duration or 0) * 1000)
        if duration_ms <= 0:
            return None
        ranked: list[tuple[float, int, int, KugouLyricCandidate]] = []
        items: list[KugouLyricCandidate] = []
        seen_ids: set[str] = set()
        for artist in artist_search_names(track.artist):
            for title in title_search_names(track.title):
                self.check_budget()
                found = self.kugou_search(
                    title=title,
                    artist=artist,
                    duration_ms=duration_ms,
                )
                self.observe_source("kugou_official_reference", "search", title=title,
                    artist=artist, result_count=len(found))
                for item in found:
                    if item.lyric_id not in seen_ids:
                        seen_ids.add(item.lyric_id)
                        items.append(item)
        for item in items:
            duration_delta = abs(float(track.duration) - item.duration_ms / 1000.0)
            reasons = []
            if not item.is_official:
                reasons.append("not_official")
            if not title_matches(track.title, item.title):
                reasons.append("title_mismatch")
            if not artist_matches(track.artist, item.artist):
                reasons.append("artist_mismatch")
            if duration_delta > 0.75:
                reasons.append("duration_mismatch")
            self.observe_source("kugou_official_reference", "identity_rejected" if reasons else "identity_accepted",
                provider_id=item.lyric_id, title=item.title, artist=item.artist, reasons=reasons,
                duration_delta_seconds=round(duration_delta, 6), official=item.is_official)
            if (
                not item.is_official
                or not title_matches(track.title, item.title)
                or not artist_matches(track.artist, item.artist)
                or duration_delta > 0.75
            ):
                continue
            ranked.append((duration_delta, -item.hit_layer, -item.score, item))
        for duration_delta, _hit_layer, _score, item in sorted(
            ranked,
            key=lambda value: value[:3],
        )[:3]:
            self.check_budget()
            lines = parse_krc(
                self.kugou_fetch(item),
                title=item.title,
                artist=item.artist,
            )
            reference_text = "".join(line.text for line in lines)
            if (
                len(lines) < 4
                or len(normalize_text(reference_text)) < 40
                or lines[-1].end > float(track.duration) + 0.5
            ):
                continue
            return {
                "reference_path": f"kugou:{item.lyric_id}:official-recommended-krc",
                "reference_text": reference_text,
                "reference_lines": [
                    {"text": line.text, "begin": line.begin, "end": line.end}
                    for line in lines
                ],
                "reference_provider": "kugou_official_krc",
                "reference_provider_id": item.lyric_id,
                "reference_identity": {
                    "title": item.title,
                    "artist": item.artist,
                    "duration_ms": item.duration_ms,
                    "duration_delta_seconds": round(duration_delta, 6),
                    "product_from": item.product_from,
                },
            }
        return None

    def _kugou_candidates(self, track: Any) -> Iterable[Candidate]:
        payload = self.gateway.alignment_payload(track, {})
        references = payload.get('reference_lines') or []
        if not references or any(line.get('end') is None for line in references):
            self.observe_source("kugou_reference_fusion", "skipped", reason="complete_reference_windows_missing")
            return
        seen = set()
        items = []
        for artist in artist_search_names(track.artist):
            for title in title_search_names(track.title):
                self.check_budget()
                found = self.kugou_search(title=title, artist=artist,
                                         duration_ms=round(track.duration * 1000))
                self.observe_source("kugou_reference_fusion", "search", title=title,
                    artist=artist, result_count=len(found))
                for item in found:
                    if item.lyric_id in seen:
                        continue
                    seen.add(item.lyric_id)
                    duration_delta = abs(track.duration - item.duration_ms / 1000)
                    reasons = []
                    if not item.is_official:
                        reasons.append("not_official")
                    if not title_matches(track.title, item.title):
                        reasons.append("title_mismatch")
                    if not artist_matches(track.artist, item.artist):
                        reasons.append("artist_mismatch")
                    if duration_delta > 3:
                        reasons.append("duration_mismatch")
                    self.observe_source("kugou_reference_fusion", "identity_rejected" if reasons else "identity_accepted",
                        provider_id=item.lyric_id, title=item.title, artist=item.artist, reasons=reasons,
                        duration_delta_seconds=round(duration_delta, 6), official=item.is_official)
                    if (item.is_official and title_matches(track.title, item.title)
                            and artist_matches(track.artist, item.artist)
                            and abs(track.duration - item.duration_ms / 1000) <= 3):
                        items.append(item)
        for item in sorted(items, key=lambda item: (abs(track.duration - item.duration_ms / 1000), -item.score))[:3]:
            self.check_budget()
            krc = self.kugou_fetch(item)
            try:
                ttml, metrics = fuse_reference_words(references, parse_krc(krc, title=item.title, artist=item.artist),
                    duration=track.duration, title=track.title, artist=track.artist)
            except (ValueError, TypeError, RuntimeError) as exc:
                self.observe_source("kugou_reference_fusion", "fusion_rejected", provider_id=item.lyric_id,
                    reason=_SOURCE_FUSION_REASONS.get(str(exc), "source_parse_or_fusion_rejected"),
                    error_type=type(exc).__name__)
                continue
            self.observe_source("kugou_reference_fusion", "fusion_accepted", provider_id=item.lyric_id,
                reference_line_count=len(references))
            identity = {key: value for key, value in asdict(item).items() if key != 'access_key'}
            yield Candidate('kugou_reference_fusion', item.lyric_id, ttml, False,
                            'native_reference_word_clock_fusion',
                            {**metrics, 'fusion_evidence': {'identity': identity, 'krc': krc}})

    def resolve(
        self,
        *,
        media_path: Path,
        metadata: dict[str, Any],
        expected_fingerprint: dict[str, Any],
    ) -> Resolution:
        self.netease_records = {}
        self.source_diagnostics = []
        self.source_diagnostic_counts = {}
        self.deadline = time.monotonic() + max(30, float(os.environ.get("WORD_LYRICS_SOURCE_BUDGET_SECONDS", "180")))
        track = self.gateway.inspect(media_path, metadata)
        existing = self.gateway.existing_word(media_path, float(track.duration or 0))
        if existing is not None:
            if media_fingerprint(media_path) != expected_fingerprint:
                raise CandidateRejected("audio_changed_since_enqueue")
            return Resolution(existing=existing, reason="existing_word_sidecar")
        rejections: list[dict[str, str]] = []
        provider_failures: list[dict[str, str]] = []
        for provider_name, candidates in (
            ("amll", self._amll_candidates(track)),
            ("netease_yrc", self._netease_candidates(track)),
            ("kugou_reference_fusion", self._kugou_candidates(track)),
        ):
            try:
                for candidate in candidates:
                    try:
                        publication = self.gateway.publish(
                            track,
                            candidate,
                            expected_fingerprint=expected_fingerprint,
                        )
                        return Resolution(
                            publication=publication,
                            reason=f"accepted_{provider_name}",
                        )
                    except CandidateRejected as exc:
                        rejections.append(
                            {
                                "provider": provider_name,
                                "provider_id": candidate.provider_id,
                                "reason": str(exc),
                            }
                        )
            except Exception as exc:
                failure = self._provider_failure(provider_name, exc)
                provider_failures.append(failure)
                rejections.append(
                    failure
                )
        payload = self.gateway.alignment_payload(track, expected_fingerprint)
        payload["source_rejections"] = rejections
        if not normalize_text(str(payload.get("reference_text") or "")):
            try:
                kugou_reference = self._kugou_reference(track)
                if kugou_reference is not None:
                    payload.update(kugou_reference)
            except Exception as exc:
                failure = self._provider_failure("kugou_official_reference", exc)
                provider_failures.append(failure)
                rejections.append(failure)
                payload["source_rejections"] = rejections
        identity_lookup = getattr(self.gateway, "localized_artist_identity", None)
        verified_artist_names: list[str] = []
        if not normalize_text(str(payload.get("reference_text") or "")) and identity_lookup:
            try:
                self.check_budget()
                identity = identity_lookup(track)
                verified_artist_names = list(identity.get("names") or [])
                payload["artist_identity_evidence"] = identity.get("evidence") or []
                already_searched = {normalize_text(name) for name in artist_search_names(track.artist)}
                for name in identity.get("names") or []:
                    if normalize_text(name) in already_searched:
                        continue
                    already_searched.add(normalize_text(name))
                    variant = replace(track, artist=name)
                    for candidates in (self._amll_candidates(variant), self._netease_candidates(variant)):
                        try:
                            for candidate in candidates:
                                try:
                                    publication = self.gateway.publish(
                                        track, candidate, expected_fingerprint=expected_fingerprint,
                                    )
                                    return Resolution(publication=publication, reason="accepted_localized_artist_source")
                                except CandidateRejected as exc:
                                    rejections.append({"provider": candidate.provider, "reason": str(exc)})
                        except Exception as exc:
                            provider_failures.append(self._provider_failure("localized_word_source", exc))
                    reference = self._kugou_reference(variant)
                    if reference is not None:
                        payload.update(reference)
                        break
            except Exception as exc:
                failure = self._provider_failure("apple_artist_identity", exc)
                provider_failures.append(failure)
                rejections.append(failure)
        payload["source_observations"] = self.source_observations()
        reference_lookup = getattr(self.gateway, "netease_reference", None)
        if not normalize_text(str(payload.get("reference_text") or "")) and reference_lookup:
            for song, record in self.netease_records.values():
                if not exact_netease_record(track, verified_artist_names, song):
                    continue
                reference = reference_lookup(track, song, record)
                if reference is not None:
                    payload.update(reference)
                    break
        if normalize_text(str(payload.get("reference_text") or "")):
            return Resolution(
                alignment_payload=payload,
                reason=(
                    "trusted_reference_needs_acoustic_alignment"
                    if payload.get("reference_provider")
                    else "local_reference_needs_acoustic_alignment"
                ),
            )
        instrumental_lookup = getattr(self.gateway, "instrumental_result", None)
        if instrumental_lookup:
            try:
                result = instrumental_lookup(track, expected_fingerprint, verified_artist_names,
                                             netease_records=list(self.netease_records.values()),
                                             check_budget=self.check_budget)
                if result is not None:
                    return Resolution(not_required=result, reason="confirmed_instrumental")
            except Exception as exc:
                failure = self._provider_failure("instrumental_verification", exc)
                provider_failures.append(failure)
                rejections.append(failure)
                payload["source_rejections"] = rejections
        faults = [item["fault"] for item in provider_failures]
        # A discovery result with the wrong title is ordinary absence, not an
        # outage. An actual candidate rejected by the strict publisher means
        # that identity/reference input is required; never weaken that gate.
        for item in rejections:
            if "fault" not in item and item.get("provider_id"):
                faults.append(LyricFault("identity", "recording_identity_unverified",
                                        provider=str(item.get("provider") or "")).as_dict())
        if not faults:
            faults = [LyricFault("no_source", "no_trusted_lyric_reference", action="recheck").as_dict()]
        payload["source_faults"] = faults
        primary = choose_fault(faults)
        reason = ("online_source_temporarily_unavailable" if primary["category"] == "transport"
                  and primary.get("retryable") else
                  "source_budget_exhausted" if primary["category"] == "resource_budget" else
                  "no_trusted_lyric_reference" if primary["category"] == "no_source" else
                  "source_attention_required")
        return Resolution(alignment_payload=payload, reason=reason,
                          retryable_error=json.dumps(provider_failures, ensure_ascii=False, sort_keys=True)
                              if provider_failures else "",
                          faults=tuple(faults))

    def validate_mac_candidate(
        self,
        *,
        media_path: Path,
        metadata: dict[str, Any],
        expected_fingerprint: dict[str, Any],
        candidate_path: Path,
        report: dict[str, Any],
        alignment_payload: dict[str, Any],
    ) -> Publication:
        if report.get("source_fingerprint") != expected_fingerprint:
            raise CandidateRejected("mac_report_fingerprint_mismatch")
        if not candidate_path.is_file() or candidate_path.stat().st_size > 16 * 1024 * 1024:
            raise CandidateRejected("candidate_file_missing_or_too_large")
        ttml = candidate_path.read_text(encoding="utf-8", errors="strict")
        if str(report.get("mode") or "") != "reference_aligned":
            raise CandidateRejected("free_transcription_without_reference_not_publishable")
        expected_text = normalize_text(
            str(alignment_payload.get("reference_text") or "")
        )
        if not expected_text:
            raise CandidateRejected("trusted_reference_text_missing")
        candidate_text = normalize_text(
            "".join(line.text for line in parse_word_ttml(ttml))
        )
        if candidate_text != expected_text:
            raise CandidateRejected("candidate_text_not_exactly_equal_to_alignment_reference")
        track = self.gateway.inspect(media_path, metadata)
        alignment_engine = str(report.get("alignment_engine") or "")
        if alignment_engine == "whisper.cpp":
            provider = "nas_whispercpp"
            provider_id = str(report.get("model") or "whisper.cpp")
        else:
            provider = "mac_mlx_whisper"
            provider_id = str(report.get("model") or "mlx-whisper")
        candidate = Candidate(
            provider=provider,
            provider_id=provider_id,
            ttml=ttml,
            exact_identity=True,
            identity_reason="acoustic_same_file",
            details={},
        )
        return self.gateway.publish(
            track,
            candidate,
            expected_fingerprint=expected_fingerprint,
            mac_report=report,
            alignment_payload=alignment_payload,
        )
