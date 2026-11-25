"""A narrow evaluation catalog; live Edge voice availability is not assumed."""

from app.tts.base import TTSError

_VOICES = (
    ("en-US-AriaNeural", "en", "Aria (English, US)", "Female"),
    ("en-US-GuyNeural", "en", "Guy (English, US)", "Male"),
    ("zh-CN-XiaoxiaoNeural", "zh-CN", "晓晓 (普通话)", "Female"),
    ("zh-CN-YunxiNeural", "zh-CN", "云希 (普通话)", "Male"),
)
_DEFAULTS = {"en": "en-US-AriaNeural", "zh-CN": "zh-CN-XiaoxiaoNeural"}


def voices_catalog() -> list[dict]:
    return [
        {
            "id": identifier,
            "language": language,
            "name": name,
            "gender": gender,
            "provider": "edge",
            "availability": "unverified_live",
        }
        for identifier, language, name, gender in _VOICES
    ]


def validate_voice(language: str, voice: str | None = None) -> str:
    if language not in _DEFAULTS:
        raise TTSError("unsupported_narration_language")
    selected = _DEFAULTS[language] if voice is None else voice
    if not any(identifier == selected and lang == language for identifier, lang, _, _ in _VOICES):
        raise TTSError("unsupported_narration_voice")
    return selected
