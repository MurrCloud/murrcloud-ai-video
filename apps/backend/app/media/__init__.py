"""FFmpeg-only composition and strict media inspection."""

from app.media.composer import compose
from app.media.contracts import (
    CompositionRequest,
    CompositionResult,
    MediaError,
    ProbeResult,
    SceneMedia,
)
from app.media.probe import probe_media, validate_output

__all__ = [
    "CompositionRequest",
    "CompositionResult",
    "MediaError",
    "ProbeResult",
    "SceneMedia",
    "compose",
    "probe_media",
    "validate_output",
]
