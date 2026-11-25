"""Secret-free, resource-bounded network child for the optional Edge adapter."""

import asyncio
import ctypes
import json
import os
import resource
import signal
import struct
import sys
from dataclasses import asdict

from app.tts.base import (
    MAX_AUDIO_BYTES,
    MAX_BOUNDARIES,
    SpeechBoundary,
    SpeechResult,
    TTSError,
    speaking_rate_to_percent,
    validate_boundaries,
    validate_text,
)
from app.tts.edge import _validate_mp3
from app.tts.voices import validate_voice

TICKS_PER_SECOND = 10_000_000
MAX_INPUT_BYTES = 32 * 1024


def install_parent_death_signal(expected_parent: int):
    """Bind this child to its actual Celery parent before any network import."""
    if type(expected_parent) is not int or expected_parent <= 1:
        raise TTSError("tts_parent_unavailable")
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise TTSError("tts_parent_unavailable")
    # The parent might have died between fork and prctl. Never contact the
    # service if the expected process has already disappeared.
    if os.getppid() != expected_parent:
        raise TTSError("tts_parent_unavailable")


async def synthesize_stream(request: dict, *, communicate_factory=None) -> SpeechResult:
    """Consume the public upstream streaming contract; tests inject only a mock."""
    if not isinstance(request, dict):
        raise TTSError("tts_invalid_stream")
    text = validate_text(request.get("text"))
    language = request.get("language")
    voice = validate_voice(language, request.get("voice"))
    rate = speaking_rate_to_percent(request.get("rate"))
    deadline = request.get("timeout_seconds", 60)
    max_bytes = request.get("max_audio_bytes", MAX_AUDIO_BYTES)
    if type(deadline) not in {int, float} or not 1 <= deadline <= 60:
        raise TTSError("tts_invalid_stream")
    if type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_AUDIO_BYTES:
        raise TTSError("tts_invalid_stream")
    if communicate_factory is None:
        try:
            from edge_tts import Communicate
        except ImportError:
            raise TTSError("tts_unavailable") from None
        communicate_factory = Communicate
    boundaries, audio, stream = [], bytearray(), None
    metadata_characters = 0
    try:
        async with asyncio.timeout(deadline):
            communicator = communicate_factory(
                text,
                voice,
                rate=rate,
                boundary="WordBoundary",
                proxy=None,
                connect_timeout=min(10, max(1, int(deadline))),
                receive_timeout=min(15, max(1, int(deadline))),
            )
            stream = communicator.stream()
            async for chunk in stream:
                if not isinstance(chunk, dict):
                    raise TTSError("tts_invalid_stream")
                if chunk.get("type") == "audio":
                    data = chunk.get("data")
                    if not isinstance(data, bytes) or not data:
                        raise TTSError("tts_invalid_stream")
                    if len(audio) + len(data) > max_bytes:
                        raise TTSError("tts_audio_too_large")
                    audio.extend(data)
                elif chunk.get("type") == "WordBoundary":
                    offset, duration, word = (
                        chunk.get("offset"),
                        chunk.get("duration"),
                        chunk.get("text"),
                    )
                    if type(offset) not in {int, float} or type(duration) not in {int, float}:
                        raise TTSError("invalid_speech_boundaries")
                    boundary = validate_boundaries(
                        [
                            SpeechBoundary(
                                offset / TICKS_PER_SECOND,
                                (offset + duration) / TICKS_PER_SECOND,
                                word,
                            )
                        ]
                    )[0]
                    if boundaries and boundary.start_seconds < boundaries[-1].end_seconds - 0.001:
                        raise TTSError("invalid_speech_boundaries")
                    metadata_characters += len(boundary.text)
                    if metadata_characters > 10_000:
                        raise TTSError("speech_metadata_too_large")
                    boundaries.append(boundary)
                    if len(boundaries) > MAX_BOUNDARIES:
                        raise TTSError("speech_metadata_too_large")
                else:
                    # Sentence-only metadata does not satisfy requested word
                    # alignment. Do not silently invent character timestamps.
                    raise TTSError("tts_invalid_stream")
            if not audio:
                raise TTSError("tts_empty_audio")
            _validate_mp3(bytes(audio))
            return SpeechResult(audio=bytes(audio), boundaries=validate_boundaries(boundaries))
    except TimeoutError:
        raise TTSError("tts_timeout", retryable=True) from None
    except TTSError:
        raise
    except Exception:
        raise TTSError("tts_unavailable", retryable=True) from None
    finally:
        if stream is not None and hasattr(stream, "aclose"):
            await stream.aclose()


def _reply(value):
    if isinstance(value, SpeechResult):
        header = {
            "format": value.format,
            "audio_bytes": len(value.audio),
            "boundaries": [asdict(boundary) for boundary in value.boundaries],
        }
        body = value.audio
    else:
        header, body = {"error": value}, b""
    data = json.dumps(header, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > 1024**2:
        data, body = b'{"error":"speech_metadata_too_large"}', b""
    sys.stdout.buffer.write(struct.pack("!I", len(data)) + data + body)


def main():
    if len(sys.argv) != 2:
        raise TTSError("tts_parent_unavailable")
    try:
        expected_parent = int(sys.argv[1])
    except ValueError:
        raise TTSError("tts_parent_unavailable") from None
    install_parent_death_signal(expected_parent)
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (192 * 1024**2, 192 * 1024**2))
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise TTSError("tts_invalid_stream")
    request = json.loads(raw)
    _reply(asyncio.run(synthesize_stream(request)))


if __name__ == "__main__":
    try:
        main()
    except TTSError as exc:
        _reply(exc.code)
        raise SystemExit(2) from None
    except Exception:
        _reply("tts_unavailable")
        raise SystemExit(2) from None
