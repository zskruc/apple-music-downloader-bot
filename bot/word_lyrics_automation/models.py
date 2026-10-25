from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TimedToken:
    text: str
    begin: float
    end: float

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("timed token text is empty")
        if self.begin < 0:
            raise ValueError("timed token begins before zero")
        if self.end <= self.begin:
            raise ValueError("timed token duration is not positive")


@dataclass(frozen=True, slots=True)
class LyricLine:
    text: str
    begin: float
    end: float
    tokens: tuple[TimedToken, ...] = field(default_factory=tuple)
    # Native line identity and voice survive every alignment/render round trip.
    key: str | None = None
    agent: str | None = None
    metadata: str | None = None

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("lyric line text is empty")
        if self.begin < 0:
            raise ValueError("lyric line begins before zero")
        if self.end <= self.begin:
            raise ValueError("lyric line duration is not positive")
        previous_end = self.begin
        for token in self.tokens:
            if token.begin + 1e-6 < self.begin:
                raise ValueError("token begins before its line")
            if token.end > self.end + 1e-6:
                raise ValueError("token ends after its line")
            if token.begin + 1e-6 < previous_end:
                raise ValueError("token timing is not monotonic")
            previous_end = token.end
