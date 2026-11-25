"""Replaceable speech adapters and speech-aligned subtitle contracts."""

from app.tts.base import (
    SpeechBoundary,
    SpeechResult,
    TTSError,
    TTSProvider,
    speaking_rate_to_percent,
    validate_boundaries,
)
from app.tts.edge import EdgeTTS
from app.tts.subtitles import (
    SubtitleCue,
    SubtitleScene,
    build_subtitles,
    safe_subtitle_text,
    to_srt,
    validate_timing,
)
from app.tts.voices import validate_voice, voices_catalog

__all__ = [
    "EdgeTTS",
    "SpeechBoundary",
    "SpeechResult",
    "SubtitleCue",
    "SubtitleScene",
    "TTSError",
    "TTSProvider",
    "build_subtitles",
    "safe_subtitle_text",
    "speaking_rate_to_percent",
    "to_srt",
    "validate_boundaries",
    "validate_timing",
    "validate_voice",
    "voices_catalog",
]
