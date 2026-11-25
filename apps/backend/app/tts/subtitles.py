"""Speech-boundary subtitles. Text grouping never invents word timestamps."""

import math
import unicodedata
from dataclasses import dataclass

from app.tts.base import SpeechBoundary, TTSError, finite_seconds, validate_boundaries

MAX_SUBTITLE_BYTES = 1024**2
MAX_CUES = 10_000


@dataclass(frozen=True)
class SubtitleCue:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True)
class SubtitleScene:
    boundaries: list[SpeechBoundary]
    duration_seconds: float
    audio_duration_seconds: float | None = None


def safe_subtitle_text(value: str) -> str:
    if not isinstance(value, str):
        raise TTSError("invalid_subtitle_text")
    # libass accepts both SRT HTML markup and ASS override syntax. Use visible
    # fullwidth punctuation for those control characters so user text is plain.
    value = value.translate(
        str.maketrans({"<": "＜", ">": "＞", "{": "｛", "}": "｝", "\\": "＼", "&": "＆"})
    )
    value = "".join(" " if unicodedata.category(c).startswith("C") else c for c in value)
    value = " ".join(value.split())
    if not value or len(value) > 5000:
        raise TTSError("invalid_subtitle_text")
    return value


def validate_timing(boundaries, scene_duration_seconds, audio_duration_seconds=None):
    validated = validate_boundaries(boundaries)
    duration = finite_seconds(scene_duration_seconds, positive=True)
    if duration > 3600:
        raise TTSError("invalid_scene_duration")
    if audio_duration_seconds is not None:
        audio_duration = finite_seconds(audio_duration_seconds, positive=True)
        if audio_duration > duration + 0.15:
            raise TTSError("narration_exceeds_scene_duration")
        if validated[-1].end_seconds > audio_duration + 0.15:
            raise TTSError("speech_boundaries_exceed_audio")
    if validated[-1].end_seconds > duration + 0.001:
        raise TTSError("narration_exceeds_scene_duration")
    return validated


def _join_tokens(tokens):
    text = ""
    for token in tokens:
        if not text:
            text = token
            continue
        # Han and other East Asian wide characters do not require inserted spaces.
        wide = unicodedata.east_asian_width(text[-1]) in {"W", "F"} or unicodedata.east_asian_width(
            token[0]
        ) in {"W", "F"}
        punctuation = unicodedata.category(token[0]).startswith("P")
        text += ("" if wide or punctuation else " ") + token
    return text


def build_subtitles(
    scenes: list[SubtitleScene],
    *,
    opening_offset_seconds: float = 0,
    max_characters: int = 42,
    max_cue_seconds: float = 3.5,
) -> list[SubtitleCue]:
    offset = finite_seconds(opening_offset_seconds)
    if offset > 10 or type(max_characters) is not int or not 10 <= max_characters <= 120:
        raise TTSError("invalid_subtitle_settings")
    if (
        type(max_cue_seconds) not in {int, float}
        or not math.isfinite(max_cue_seconds)
        or not 0.5 <= max_cue_seconds <= 10
    ):
        raise TTSError("invalid_subtitle_settings")
    if not isinstance(scenes, list) or len(scenes) > 120:
        raise TTSError("invalid_subtitle_settings")
    cues = []
    for scene in scenes:
        if not isinstance(scene, SubtitleScene):
            raise TTSError("invalid_subtitle_settings")
        duration = finite_seconds(scene.duration_seconds, positive=True)
        if not scene.boundaries:
            # A scene without narration contributes its entire duration to the
            # timeline; callers must not turn missing speech metadata into this.
            offset += duration
            if offset > 7200:
                raise TTSError("subtitle_timeline_too_large")
            continue
        boundaries = validate_timing(scene.boundaries, duration, scene.audio_duration_seconds)
        group = []
        for boundary in boundaries:
            text = safe_subtitle_text(boundary.text)
            if group and (
                len(_join_tokens([item[2] for item in group] + [text])) > max_characters
                or boundary.end_seconds - group[0][0] > max_cue_seconds
                or boundary.start_seconds - group[-1][1] > 0.6
            ):
                cues.append(
                    SubtitleCue(
                        offset + group[0][0],
                        offset + group[-1][1],
                        _join_tokens([item[2] for item in group]),
                    )
                )
                group = []
            group.append((boundary.start_seconds, boundary.end_seconds, text))
        if group:
            cues.append(
                SubtitleCue(
                    offset + group[0][0],
                    offset + group[-1][1],
                    _join_tokens([item[2] for item in group]),
                )
            )
        offset += duration
        if offset > 7200 or len(cues) > MAX_CUES:
            raise TTSError("subtitle_timeline_too_large")
    return cues


def _timestamp(seconds: float) -> str:
    value = round(finite_seconds(seconds) * 1000)
    if value >= 100 * 3600 * 1000:
        raise TTSError("invalid_subtitle_timing")
    hours, remainder = divmod(value, 3600 * 1000)
    minutes, remainder = divmod(remainder, 60 * 1000)
    whole, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{millis:03d}"


def to_srt(cues: list[SubtitleCue]) -> str:
    if not isinstance(cues, list) or len(cues) > MAX_CUES:
        raise TTSError("subtitle_timeline_too_large")
    lines = []
    previous_end = 0.0
    byte_count = 0
    for index, cue in enumerate(cues, start=1):
        if not isinstance(cue, SubtitleCue):
            raise TTSError("invalid_subtitle_timing")
        start, end = finite_seconds(cue.start_seconds), finite_seconds(cue.end_seconds)
        if start < previous_end - 0.001 or end <= start or round(end * 1000) <= round(start * 1000):
            raise TTSError("invalid_subtitle_timing")
        text = safe_subtitle_text(cue.text)
        block = f"{index}\n{_timestamp(start)} --> {_timestamp(end)}\n{text}\n\n"
        byte_count += len(block.encode("utf-8"))
        if byte_count > MAX_SUBTITLE_BYTES:
            raise TTSError("subtitle_timeline_too_large")
        lines.append(block)
        previous_end = end
    return "".join(lines)
