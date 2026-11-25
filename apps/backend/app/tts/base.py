"""Provider-independent speech contracts and bounded validation."""

import math
import unicodedata
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Protocol

MAX_TEXT_CHARACTERS = 5000
MAX_AUDIO_BYTES = 10 * 1024**2
MAX_BOUNDARIES = 10_000
MAX_SPEECH_SECONDS = 3600


class TTSError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True)
class SpeechBoundary:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True)
class SpeechResult:
    audio: bytes
    format: str = "mp3"
    boundaries: list[SpeechBoundary] = field(default_factory=list)


class TTSProvider(Protocol):
    async def synthesize(
        self, text: str, voice: str, rate: float, language: str, *, approved: bool = False
    ) -> SpeechResult: ...

    async def aclose(self) -> None: ...


def validate_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARACTERS:
        raise TTSError("invalid_narration_text")
    if any(unicodedata.category(c).startswith("C") and c not in "\n\r\t" for c in text):
        raise TTSError("invalid_narration_text")
    return text


def speaking_rate_to_percent(rate) -> str:
    if isinstance(rate, bool):
        raise TTSError("invalid_speaking_rate")
    try:
        value = Decimal(str(rate))
        if not value.is_finite() or not Decimal("0.5") <= value <= Decimal("2"):
            raise ValueError
        # Project settings retain two decimal places, so avoid a silent mismatch
        # between an input fingerprint and the effective provider percentage.
        if value != value.quantize(Decimal("0.01")):
            raise ValueError
        percent = ((value - 1) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return f"{int(percent):+d}%"
    except (ValueError, InvalidOperation):
        raise TTSError("invalid_speaking_rate") from None


def finite_seconds(value, *, positive=False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TTSError("invalid_speech_boundaries")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise TTSError("invalid_speech_boundaries") from None
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        raise TTSError("invalid_speech_boundaries")
    return number


def validate_boundaries(boundaries: list[SpeechBoundary]) -> list[SpeechBoundary]:
    if not isinstance(boundaries, list) or not 1 <= len(boundaries) <= MAX_BOUNDARIES:
        raise TTSError("speech_boundaries_unavailable")
    result = []
    previous_end = 0.0
    total_text = 0
    for boundary in boundaries:
        if not isinstance(boundary, SpeechBoundary):
            raise TTSError("invalid_speech_boundaries")
        start = finite_seconds(boundary.start_seconds)
        end = finite_seconds(boundary.end_seconds, positive=True)
        if end <= start or start < previous_end - 0.001 or end > MAX_SPEECH_SECONDS:
            raise TTSError("invalid_speech_boundaries")
        if not isinstance(boundary.text, str) or not boundary.text.strip():
            raise TTSError("invalid_speech_boundaries")
        total_text += len(boundary.text)
        if len(boundary.text) > MAX_TEXT_CHARACTERS or total_text > MAX_TEXT_CHARACTERS * 2:
            raise TTSError("speech_metadata_too_large")
        result.append(SpeechBoundary(start, end, boundary.text))
        previous_end = end
    return result
