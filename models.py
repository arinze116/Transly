"""Provider-independent transcript and normalized media models."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class NormalizedMedia:
    local_path: Path
    media_type: str = "audio"
    mime_type: str = "audio/wav"
    duration: float | None = None
    size_bytes: int = 0


@dataclass
class TranscriptSegment:
    start: float
    end: float
    speaker: str
    text: str
    english_text: str | None = None


@dataclass
class TranscriptResult:
    language: str
    duration: float
    segments: list[TranscriptSegment] = field(default_factory=list)
    provider: str = "unknown"

    @property
    def speaker_count(self) -> int:
        return len({segment.speaker for segment in self.segments})
