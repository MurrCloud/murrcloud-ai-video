"""Isolated Edge evaluation adapter; no network request happens in this process."""

import asyncio
import contextlib
import json
import os
import signal
import struct
import sys

from app.tts.base import (
    MAX_AUDIO_BYTES,
    SpeechBoundary,
    SpeechResult,
    TTSError,
    speaking_rate_to_percent,
    validate_boundaries,
    validate_text,
)
from app.tts.voices import validate_voice

MAX_HEADER_BYTES = 1024**2
_SAFE_ERRORS = {
    "tts_unavailable",
    "tts_timeout",
    "tts_audio_too_large",
    "tts_empty_audio",
    "tts_invalid_audio",
    "tts_invalid_stream",
    "speech_boundaries_unavailable",
    "invalid_speech_boundaries",
    "speech_metadata_too_large",
}


class EdgeTTS:
    def __init__(self, *, timeout_seconds=60, max_audio_bytes=MAX_AUDIO_BYTES):
        if type(timeout_seconds) not in {int, float} or not 1 <= timeout_seconds <= 60:
            raise ValueError("Invalid TTS deadline")
        if type(max_audio_bytes) is not int or not 1024 <= max_audio_bytes <= MAX_AUDIO_BYTES:
            raise ValueError("Invalid TTS audio bound")
        self.timeout_seconds = timeout_seconds
        self.max_audio_bytes = max_audio_bytes

    async def synthesize(
        self, text: str, voice: str | None, rate, language: str, *, approved: bool = False
    ) -> SpeechResult:
        if approved is not True:
            raise TTSError("narration_not_approved")
        validate_text(text)
        selected = validate_voice(language, voice)
        speaking_rate_to_percent(rate)
        request = json.dumps(
            {
                "text": text,
                "voice": selected,
                "rate": str(rate),
                "language": language,
                "timeout_seconds": self.timeout_seconds,
                "max_audio_bytes": self.max_audio_bytes,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        process = None
        try:
            async with asyncio.timeout(self.timeout_seconds + 3):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app.tts.edge_worker",
                    str(os.getpid()),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                    env={
                        "PATH": os.defpath,
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONUTF8": "1",
                        "OMP_NUM_THREADS": "1",
                        "NETRC": "/nonexistent",
                    },
                )
                process.stdin.write(request)
                await process.stdin.drain()
                process.stdin.close()
                size = struct.unpack("!I", await process.stdout.readexactly(4))[0]
                if not 1 <= size <= MAX_HEADER_BYTES:
                    raise TTSError("tts_invalid_stream")
                header = json.loads(await process.stdout.readexactly(size))
                if not isinstance(header, dict):
                    raise TTSError("tts_invalid_stream")
                if "error" in header:
                    code = header.get("error")
                    raise TTSError(
                        code if code in _SAFE_ERRORS else "tts_unavailable",
                        retryable=code in {"tts_unavailable", "tts_timeout"},
                    )
                audio_size = header.get("audio_bytes")
                if type(audio_size) is not int or not 1 <= audio_size <= self.max_audio_bytes:
                    raise TTSError("tts_audio_too_large")
                if header.get("format") != "mp3" or not isinstance(header.get("boundaries"), list):
                    raise TTSError("tts_invalid_stream")
                raw_boundaries = header["boundaries"]
                if len(raw_boundaries) > 10_000:
                    raise TTSError("speech_metadata_too_large")
                boundaries = validate_boundaries(
                    [
                        SpeechBoundary(item["start_seconds"], item["end_seconds"], item["text"])
                        for item in raw_boundaries
                    ]
                )
                audio = await process.stdout.readexactly(audio_size)
                if await process.stdout.read(1) or await process.wait() != 0:
                    raise TTSError("tts_invalid_stream")
                _validate_mp3(audio)
                return SpeechResult(audio=audio, format="mp3", boundaries=boundaries)
        except TimeoutError:
            raise TTSError("tts_timeout", retryable=True) from None
        except TTSError:
            raise
        except (OSError, ValueError, KeyError, TypeError, asyncio.IncompleteReadError):
            raise TTSError("tts_unavailable", retryable=True) from None
        finally:
            if process is not None and process.returncode is None:
                # A timeout/cancel must not leave a networked child behind. The
                # child owns its process group and receives no provider secrets.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await process.wait()

    async def aclose(self):
        return None


def _validate_mp3(audio):
    if not (
        isinstance(audio, bytes)
        and len(audio) >= 3
        and (audio.startswith(b"ID3") or (audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0))
    ):
        raise TTSError("tts_invalid_audio")
