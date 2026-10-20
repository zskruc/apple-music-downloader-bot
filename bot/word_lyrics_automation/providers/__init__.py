"""Online word-timed lyric providers."""
from .amll import AMLLClient, AMLLLyric
from .kugou import KugouLyricCandidate
from .netease import NeteaseSong

__all__ = ["AMLLClient", "AMLLLyric", "KugouLyricCandidate", "NeteaseSong"]
