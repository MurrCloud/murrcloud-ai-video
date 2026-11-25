"""Bounded, protocol-restricted FFprobe and complete decode verification."""

import json
import math
from fractions import Fraction
from pathlib import Path

from app.media.contracts import MediaError, ProbeResult
from app.media.process import ProcessRunner, checked_file


def input_format(path):
    with Path(path).open("rb") as source:
        prefix = source.read(16)
    if prefix[4:8] == b"ftyp":
        return "mov"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return "wav"
    if prefix.startswith(b"ID3") or (
        len(prefix) > 1 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0
    ):
        return "mp3"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png_pipe"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "jpeg_pipe"
    raise MediaError("media_container_rejected")


def input_args(path, *, loop=False):
    path = checked_file(path)
    format_name = input_format(path)
    arguments = [
        "-protocol_whitelist",
        "file,pipe",
        "-probesize",
        "1048576",
        "-analyzeduration",
        "5000000",
        "-f",
        format_name,
    ]
    if format_name == "mov":
        arguments += ["-enable_drefs", "0", "-use_absolute_path", "0"]
    if loop:
        arguments += ["-stream_loop", "-1"]
    return [*arguments, "-i", str(path)]


def finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def rate(value):
    try:
        parsed = float(Fraction(value))
        return parsed if math.isfinite(parsed) and 0 < parsed <= 240 else None
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def probe_media(path, *, timeout_seconds=15, runner=None):
    path = checked_file(path)
    runner = runner or ProcessRunner(timeout_seconds=timeout_seconds)
    command = [
        "ffprobe",
        "-v",
        "error",
        "-threads",
        "1",
        "-max_alloc",
        "67108864",
        *input_args(path)[:-2],
        "-show_entries",
        "format=duration,format_name,size:stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,duration,pix_fmt,sample_rate,channels",
        "-of",
        "json",
        str(path),
    ]
    try:
        document = json.loads(runner.run(command))
        streams = document["streams"]
        if not isinstance(streams, list) or not 1 <= len(streams) <= 16:
            raise ValueError
        video = next((s for s in streams if s["codec_type"] == "video"), {})
        audio = next((s for s in streams if s["codec_type"] == "audio"), {})
        duration = finite(document.get("format", {}).get("duration"))
        if duration is None:
            duration = max((finite(s.get("duration")) or 0 for s in streams), default=0)
        width, height = video.get("width"), video.get("height")
        if video and (
            type(width) is not int
            or type(height) is not int
            or min(width, height) < 1
            or max(width, height) > 8192
            or width * height > 8_388_608
        ):
            raise MediaError("source_dimensions_rejected")
        if not 0 <= duration <= 7200:
            raise MediaError("source_duration_rejected")
        result = ProbeResult(
            duration,
            width,
            height,
            rate(video.get("avg_frame_rate")) or rate(video.get("r_frame_rate")),
            video.get("codec_name"),
            audio.get("codec_name"),
            bool(video),
            bool(audio),
            {
                "duration_seconds": duration,
                "width": width,
                "height": height,
                "video_codec": video.get("codec_name"),
                "audio_codec": audio.get("codec_name"),
                "pixel_format": video.get("pix_fmt"),
                "sample_rate": audio.get("sample_rate"),
                "audio_channels": audio.get("channels"),
                "stream_count": len(streams),
                "stream_durations": [finite(s.get("duration")) for s in streams],
            },
        )
        return result
    except MediaError:
        raise
    except (ValueError, KeyError, TypeError, StopIteration):
        raise MediaError("invalid_probe_result") from None


def validate_output(
    path, expected_duration_seconds, width, height, *, fps=30, timeout_seconds=120, runner=None
):
    runner = runner or ProcessRunner(timeout_seconds=timeout_seconds)
    result = probe_media(path, runner=runner)
    durations = [d for d in result.report["stream_durations"] if d is not None]
    if (
        not result.has_video
        or not result.has_audio
        or result.video_codec != "h264"
        or result.audio_codec != "aac"
        or result.width != width
        or result.height != height
        or result.fps is None
        or abs(result.fps - fps) > 0.01
        or result.report["pixel_format"] != "yuv420p"
        or abs(result.duration_seconds - expected_duration_seconds) > 0.15
        or any(abs(duration - expected_duration_seconds) > 0.15 for duration in durations)
    ):
        raise MediaError("output_validation_failed")
    # A readable header does not establish integrity; decode every selected frame.
    runner.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-xerror",
            "-err_detect",
            "explode",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-filter_complex_threads",
            "1",
            "-max_alloc",
            "67108864",
            *input_args(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-sn",
            "-dn",
            "-threads",
            "1",
            "-f",
            "null",
            "-",
        ]
    )
    return result
