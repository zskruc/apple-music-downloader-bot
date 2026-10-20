from __future__ import annotations

import hashlib
import struct
from pathlib import Path


MP4_AUDIO_IDENTITY_METHOD = "mp4_samples_and_identity_tags_v2"


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def media_fingerprint(path: Path) -> dict[str, int | str]:
    """Return an immutable-content fingerprint used for queue idempotency."""

    stat = path.stat()
    return {
        "sha256": sha256_file(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def mp4_audio_identity(path: Path) -> dict[str, str] | None:
    """Identify unchanged M4A samples AND search-relevant tags after tagging.

    This is only a retry-budget continuity check, never a publication
    fingerprint. Full file SHA/size/mtime and every publication gate remain.
    Invalid, unsupported and unknown MP4 structures fail closed.
    """
    if path.suffix.casefold() not in {".m4a", ".mp4"}:
        return None
    try:
        with path.open("rb") as handle:
            before = path.stat()
            size = before.st_size

            def boxes(start: int, end: int) -> list[tuple[bytes, int, int]]:
                result = []
                while start < end:
                    handle.seek(start)
                    header = handle.read(8)
                    if len(header) != 8:
                        raise ValueError("truncated MP4 atom")
                    length, name = struct.unpack(">I4s", header)
                    header_size = 8
                    if length == 1:
                        extended = handle.read(8)
                        if len(extended) != 8:
                            raise ValueError("truncated extended atom")
                        length = struct.unpack(">Q", extended)[0]
                        header_size = 16
                    elif length == 0:
                        length = end - start
                    if length < header_size or start + length > end:
                        raise ValueError("invalid MP4 atom size")
                    result.append((name, start + header_size, start + length))
                    start += length
                return result

            def children(items, name):
                return [box for box in items if box[0] == name]

            def digest_box(digest, box):
                name, begin, end = box
                digest.update(name)
                digest.update(struct.pack(">Q", end - begin))
                handle.seek(begin)
                remaining = end - begin
                while remaining:
                    data = handle.read(min(remaining, 1024 * 1024))
                    if not data:
                        raise ValueError("truncated MP4 payload")
                    digest.update(data)
                    remaining -= len(data)

            top = boxes(0, size)
            moovs = children(top, b"moov")
            mdats = children(top, b"mdat")
            if len(moovs) != 1 or not mdats or children(top, b"moof"):
                return None
            moov = boxes(moovs[0][1], moovs[0][2])
            audio = hashlib.sha256()
            count = 0
            for trak in children(moov, b"trak"):
                mdias = children(boxes(trak[1], trak[2]), b"mdia")
                if len(mdias) != 1:
                    return None
                mdia = boxes(mdias[0][1], mdias[0][2])
                handlers = children(mdia, b"hdlr")
                if len(handlers) != 1:
                    return None
                handle.seek(handlers[0][1])
                if handle.read(12)[8:12] != b"soun":
                    continue
                mdhd = children(mdia, b"mdhd")
                minf = children(mdia, b"minf")
                if len(mdhd) != 1 or len(minf) != 1:
                    return None
                stbl = children(boxes(minf[0][1], minf[0][2]), b"stbl")
                if len(stbl) != 1:
                    return None
                tables = boxes(stbl[0][1], stbl[0][2])
                required = [children(tables, key) for key in (b"stsd", b"stts", b"stsc")]
                lengths = children(tables, b"stsz") + children(tables, b"stz2")
                if any(len(items) != 1 for items in required) or len(lengths) != 1:
                    return None
                for box in [mdhd[0], *(items[0] for items in required), lengths[0]]:
                    digest_box(audio, box)
                count += 1
            if count != 1:
                return None
            for box in mdats:
                digest_box(audio, box)
            metadata = hashlib.sha256()
            metas = children(moov, b"meta")
            for udta in children(moov, b"udta"):
                metas.extend(children(boxes(udta[1], udta[2]), b"meta"))
            identity_atoms = []
            def remember_identity(box):
                digest = hashlib.sha256()
                digest_box(digest, box)
                identity_atoms.append((box[0], digest.digest()))

            for meta in metas:
                for ilst in children(boxes(meta[1] + 4, meta[2]), b"ilst"):
                    for box in boxes(ilst[1], ilst[2]):
                        if box[0] in {b"\xa9nam", b"\xa9ART", b"\xa9alb", b"aART",
                                      b"cnID", b"atID", b"trkn", b"disk"}:
                            remember_identity(box)
                        elif box[0] == b"----":
                            fields = boxes(box[1], box[2])
                            names = children(fields, b"name")
                            if len(names) == 1 and names[0][2] - names[0][1] <= 1024:
                                handle.seek(names[0][1])
                                if b"isrc" in handle.read(names[0][2] - names[0][1]).lower():
                                    remember_identity(box)
            # Mutagen reorders unchanged identity atoms when it saves a gain
            # tag. Hash the complete atom multiset in a canonical order, so
            # values and duplicate counts still matter but storage order does
            # not reset source discovery.
            for name, digest in sorted(identity_atoms):
                metadata.update(name)
                metadata.update(digest)
            after = path.stat()
            if (not identity_atoms or (before.st_size, before.st_mtime_ns, before.st_ino)
                    != (after.st_size, after.st_mtime_ns, after.st_ino)):
                return None
            return {"method": MP4_AUDIO_IDENTITY_METHOD,
                    "audio_sha256": audio.hexdigest(), "metadata_sha256": metadata.hexdigest()}
    except (OSError, ValueError, struct.error):
        return None
