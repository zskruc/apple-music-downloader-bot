from __future__ import annotations

import re
from dataclasses import dataclass


ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
GAMDL_TRACK_RE = re.compile(
    r"\[Track\s+(?P<index>\d+)\s*/\s*(?P<total>\d+)\s*\]"
    r"\s+Downloading\s+\"(?P<title>.*)\""
)
GAMDL_PROGRESS_RE = re.compile(
    r"\[download\]\s+"
    r"(?P<percent>\d+(?:\.\d+)?)%\s+of\s+"
    r"(?P<estimated>~\s*)?"
    r"(?P<total>\d+(?:\.\d+)?)\s*(?P<total_unit>[KMGTPE]?i?B)"
    r"(?:"
    r"\s+at\s+(?P<speed>\d+(?:\.\d+)?)\s*"
    r"(?P<speed_unit>[KMGTPE]?i?B/s)\s+ETA\s+(?P<eta>\S+)"
    r"|"
    r"\s+in\s+(?P<duration>\S+)\s+at\s+"
    r"(?P<final_speed>\d+(?:\.\d+)?)\s*"
    r"(?P<final_speed_unit>[KMGTPE]?i?B/s)"
    r")"
    r"(?:\s+\(frag\s+(?P<fragment>\d+)\s*/\s*(?P<fragments>\d+)\))?"
)


@dataclass(frozen=True)
class GamdlTrack:
    index: int
    total: int
    title: str


@dataclass(frozen=True)
class GamdlTransfer:
    percent: float
    total_bytes: int
    total_estimated: bool
    speed_bytes_per_second: float
    eta: str
    fragment: int | None
    fragments: int | None


def strip_ansi(value: str) -> str:
    return ANSI_ESCAPE_RE.sub("", value).replace("\r", "").strip()


def parse_size(value: str, unit: str) -> float:
    normalized = unit.strip()
    if normalized.endswith("/s"):
        normalized = normalized[:-2]
    prefixes = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}
    match = re.fullmatch(r"(?P<prefix>[KMGTPE]?)(?P<binary>i?)B", normalized)
    if not match:
        raise ValueError(f"unsupported size unit: {unit}")
    exponent = prefixes[match.group("prefix")]
    base = 1024 if match.group("binary") else 1000
    return float(value) * (base**exponent)


def parse_gamdl_track(line: str) -> GamdlTrack | None:
    match = GAMDL_TRACK_RE.search(strip_ansi(line))
    if not match:
        return None
    return GamdlTrack(
        index=int(match.group("index")),
        total=int(match.group("total")),
        title=match.group("title").strip(),
    )


def parse_gamdl_transfer(line: str) -> GamdlTransfer | None:
    match = GAMDL_PROGRESS_RE.search(strip_ansi(line))
    if not match:
        return None
    speed = match.group("speed") or match.group("final_speed")
    speed_unit = match.group("speed_unit") or match.group("final_speed_unit")
    eta = match.group("eta") or "00:00"
    fragment = match.group("fragment")
    fragments = match.group("fragments")
    return GamdlTransfer(
        percent=min(100.0, max(0.0, float(match.group("percent")))),
        total_bytes=max(
            0,
            round(parse_size(match.group("total"), match.group("total_unit"))),
        ),
        total_estimated=bool(match.group("estimated")),
        speed_bytes_per_second=max(0.0, parse_size(speed, speed_unit)),
        eta=eta,
        fragment=int(fragment) if fragment is not None else None,
        fragments=int(fragments) if fragments is not None else None,
    )


def progress_bar(percent: float, width: int = 16) -> str:
    bounded = min(100.0, max(0.0, percent))
    filled = min(width, max(0, round(bounded * width / 100)))
    return "█" * filled + "░" * (width - filled)


def format_iec_size(value: float) -> str:
    size = max(0.0, float(value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if size < 1024 or candidate == units[-1]:
            break
        size /= 1024
    if unit == "B":
        return f"{int(size)} B"
    return f"{size:.2f} {unit}"
