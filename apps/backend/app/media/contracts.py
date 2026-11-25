"""Application-owned media contracts; no provider schemas or network URLs."""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


class MediaError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True)
class SceneMedia:
    path: Path
    duration_seconds: float
    transition: str = "fade"
    narration_path: Path | None = None


@dataclass(frozen=True)
class CompositionRequest:
    scenes: list[SceneMedia]
    output: Path
    scratch: Path
    aspect_ratio: str = "16:9"
    subtitles_path: Path | None = None
    burn_subtitles: bool = False
    music_path: Path | None = None
    watermark_path: Path | None = None
    opening_title: str = ""
    closing_title: str = ""
    title_duration_seconds: float = 2
    allow_upscale: bool = False
    music_volume: float = 0.15
    source_audio_volume: float = 0
    ffmpeg_threads: int = 1
    timeout_seconds: int = 3600
    max_output_bytes: int = 512 * 1024**2
    max_scratch_bytes: int = 128 * 1024**2
    progress: Callable[[int], None] | None = None
    admission: Callable[[], bool] | None = None


@dataclass(frozen=True)
class ProbeResult:
    duration_seconds: float
    width: int | None
    height: int | None
    fps: float | None
    video_codec: str | None
    audio_codec: str | None
    has_video: bool
    has_audio: bool
    report: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CompositionResult:
    path: Path
    duration_seconds: float
    width: int
    height: int
    fps: float
    warnings: list[str]
    report: dict
