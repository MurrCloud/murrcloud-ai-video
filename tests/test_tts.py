"""Speech boundary fixtures and secret-free Edge protocol tests; never call Edge."""

import asyncio
import json
import math
import struct
from dataclasses import asdict
from decimal import Decimal

import pytest
from app.tts import (
    EdgeTTS,
    SpeechBoundary,
    SubtitleCue,
    SubtitleScene,
    TTSError,
    build_subtitles,
    safe_subtitle_text,
    speaking_rate_to_percent,
    to_srt,
    validate_boundaries,
    validate_timing,
    validate_voice,
    voices_catalog,
)
from app.tts.edge_worker import synthesize_stream

WORD = {"type": "WordBoundary", "offset": 2_000_000, "duration": 4_000_000, "text": "Hello"}
AUDIO = {"type": "audio", "data": b"ID3mock-mp3-audio"}
REQUEST = {"text": "Hello world", "language": "en", "voice": "en-US-AriaNeural", "rate": "1.0"}


def run(awaitable):
    return asyncio.run(awaitable)


def communicate(chunks, calls=None, *, fail=None):
    class MockCommunicate:
        def __init__(self, *args, **kwargs):
            if calls is not None:
                calls.append((args, kwargs))

        async def stream(self):
            for chunk in chunks:
                yield chunk
            if fail is not None:
                raise fail

    return MockCommunicate


def test_edge_stream_uses_explicit_word_alignment_and_converts_ticks():
    calls = []
    result = run(
        synthesize_stream(
            REQUEST,
            communicate_factory=communicate(
                [
                    AUDIO,
                    WORD,
                    {**WORD, "offset": 8_000_000, "duration": 5_000_000, "text": "world"},
                ],
                calls,
            ),
        )
    )
    assert result.audio == AUDIO["data"] and result.format == "mp3"
    assert result.boundaries == [
        SpeechBoundary(0.2, 0.6, "Hello"),
        SpeechBoundary(0.8, 1.3, "world"),
    ]
    args, kwargs = calls[0]
    assert args == ("Hello world", "en-US-AriaNeural")
    assert kwargs["boundary"] == "WordBoundary" and kwargs["rate"] == "+0%"
    assert (
        kwargs["proxy"] is None
        and kwargs["connect_timeout"] <= 10
        and kwargs["receive_timeout"] <= 15
    )


def test_chinese_stream_keeps_actual_boundaries():
    result = run(
        synthesize_stream(
            {
                **REQUEST,
                "text": "欢迎来到云端",
                "language": "zh-CN",
                "voice": "zh-CN-XiaoxiaoNeural",
                "rate": "1.25",
            },
            communicate_factory=communicate(
                [
                    AUDIO,
                    {**WORD, "text": "欢迎"},
                    {**WORD, "text": "来到云端", "offset": 7_000_000},
                ]
            ),
        )
    )
    assert [word.text for word in result.boundaries] == ["欢迎", "来到云端"]
    assert result.boundaries[1].start_seconds == 0.7


@pytest.mark.parametrize(
    "chunks,code",
    [
        ([AUDIO], "speech_boundaries_unavailable"),
        ([WORD], "tts_empty_audio"),
        ([{"type": "audio", "data": b"not-mp3"}, WORD], "tts_invalid_audio"),
        ([AUDIO, {**WORD, "type": "SentenceBoundary"}], "tts_invalid_stream"),
        ([AUDIO, {**WORD, "offset": -1}], "invalid_speech_boundaries"),
        ([AUDIO, {**WORD, "duration": 0}], "invalid_speech_boundaries"),
        ([AUDIO, {**WORD, "offset": float("nan")}], "invalid_speech_boundaries"),
        ([AUDIO, {**WORD, "offset": True}], "invalid_speech_boundaries"),
        ([AUDIO, {**WORD, "text": ""}], "invalid_speech_boundaries"),
        ([AUDIO, {**WORD, "text": "x" * 5001}], "speech_metadata_too_large"),
        ([AUDIO, WORD, WORD], "invalid_speech_boundaries"),
        ([AUDIO, {"type": "unexpected", "token": "private-provider-body"}], "tts_invalid_stream"),
        ([AUDIO, "not-a-mapping"], "tts_invalid_stream"),
        ([{"type": "audio", "data": "not-bytes"}], "tts_invalid_stream"),
    ],
)
def test_edge_stream_fails_closed(chunks, code):
    with pytest.raises(TTSError, match=code) as error:
        run(synthesize_stream(REQUEST, communicate_factory=communicate(chunks)))
    assert "private-provider-body" not in str(error.value)


def test_edge_stream_bounds_audio_and_incremental_metadata():
    with pytest.raises(TTSError, match="tts_audio_too_large"):
        run(
            synthesize_stream(
                {**REQUEST, "max_audio_bytes": 1024},
                communicate_factory=communicate(
                    [
                        {"type": "audio", "data": b"ID3" + b"a" * 1024},
                        WORD,
                    ]
                ),
            )
        )
    chunks = [AUDIO] + [{**WORD, "offset": i * 10_000_000, "text": "x" * 5000} for i in range(3)]
    with pytest.raises(TTSError, match="speech_metadata_too_large"):
        run(synthesize_stream(REQUEST, communicate_factory=communicate(chunks)))


def test_edge_stream_exception_is_sanitized_and_retryable():
    with pytest.raises(TTSError) as error:
        run(
            synthesize_stream(
                REQUEST,
                communicate_factory=communicate([], fail=RuntimeError("private-script-secret")),
            )
        )
    assert error.value.code == "tts_unavailable" and error.value.retryable
    assert "private-script-secret" not in str(error.value)


def test_edge_stream_has_absolute_deadline_even_with_small_chunks():
    closed = []

    class Drip:
        def __init__(self, *args, **kwargs):
            pass

        async def stream(self):
            try:
                while True:
                    await asyncio.sleep(0.05)
                    yield {"type": "audio", "data": b"ID3"}
            finally:
                closed.append(True)

    with pytest.raises(TTSError, match="tts_timeout"):
        run(synthesize_stream({**REQUEST, "timeout_seconds": 1}, communicate_factory=Drip))
    assert closed == [True]


@pytest.mark.parametrize("text", ["", " ", None, "x" * 5001, "hello\x00world", "a\u202eb"])
def test_untrusted_narration_text_is_bounded_before_network(text):
    calls = []
    with pytest.raises(TTSError, match="invalid_narration_text"):
        run(
            synthesize_stream({**REQUEST, "text": text}, communicate_factory=communicate([], calls))
        )
    assert calls == []


@pytest.mark.parametrize(
    "rate,expected", [(Decimal("0.5"), "-50%"), (1, "+0%"), (1.25, "+25%"), (2, "+100%")]
)
def test_speaking_rate_is_provider_independent(rate, expected):
    assert speaking_rate_to_percent(rate) == expected


@pytest.mark.parametrize(
    "rate", [True, "0.49", "2.01", "1.001", "not-a-rate", float("nan"), float("inf"), None]
)
def test_invalid_rate_cannot_reach_provider(rate):
    with pytest.raises(TTSError, match="invalid_speaking_rate"):
        speaking_rate_to_percent(rate)


def test_voice_catalog_has_both_languages_without_claiming_live_availability():
    voices = voices_catalog()
    assert len(voices) == 4 and {voice["language"] for voice in voices} == {"en", "zh-CN"}
    assert all(voice["availability"] == "unverified_live" for voice in voices)
    assert validate_voice("en") == "en-US-AriaNeural"
    assert validate_voice("zh-CN") == "zh-CN-XiaoxiaoNeural"
    with pytest.raises(TTSError, match="unsupported_narration_voice"):
        validate_voice("zh-CN", "en-US-AriaNeural")
    with pytest.raises(TTSError, match="unsupported_narration_language"):
        validate_voice("fr")
    voices[0]["id"] = "mutated"
    assert voices_catalog()[0]["id"] == "en-US-AriaNeural"


def test_edge_adapter_cannot_spawn_without_approval(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("A nonapproved TTS request spawned a child")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(TTSError, match="narration_not_approved"):
        run(EdgeTTS().synthesize("Hello", None, 1, "en"))


class MockInput:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        pass


class MockProcess:
    def __init__(self, packet=b"", *, hanging=False):
        self.stdin = MockInput()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(packet)
        if not hanging:
            self.stdout.feed_eof()
        self.returncode = None
        self.pid = 123456789

    async def wait(self):
        self.returncode = 0
        return 0


def protocol_packet(header=None, audio=AUDIO["data"]):
    header = header or {
        "format": "mp3",
        "audio_bytes": len(audio),
        "boundaries": [asdict(SpeechBoundary(0.2, 0.6, "Hello"))],
    }
    body = json.dumps(header).encode()
    return struct.pack("!I", len(body)) + body + audio


def test_edge_adapter_runs_fixed_secret_free_child(monkeypatch):
    calls = []
    monkeypatch.setenv("VIDEO_OPENAI_API_KEY", "private-openai-key")
    monkeypatch.setenv("HTTPS_PROXY", "https://private-proxy.invalid")

    async def factory(*args, **kwargs):
        calls.append((args, kwargs))
        process = MockProcess(protocol_packet())
        calls.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", factory)
    result = run(EdgeTTS().synthesize("Hello", None, Decimal("1.25"), "en", approved=True))
    assert result.audio == AUDIO["data"] and result.boundaries == [
        SpeechBoundary(0.2, 0.6, "Hello")
    ]
    args, options = calls[0]
    assert args[1:3] == ("-m", "app.tts.edge_worker")
    assert int(args[3]) == __import__("os").getpid()
    assert options["start_new_session"] is True and options["stderr"] == asyncio.subprocess.DEVNULL
    assert "VIDEO_OPENAI_API_KEY" not in options["env"] and "HTTPS_PROXY" not in options["env"]
    assert options["env"]["NETRC"] == "/nonexistent" and "HOME" not in options["env"]
    assert "Hello" not in args and "private-openai-key" not in repr(options)
    assert json.loads(calls[1].stdin.data)["rate"] == "1.25"


@pytest.mark.parametrize(
    "packet,code",
    [
        (struct.pack("!I", 1024**2 + 1), "tts_invalid_stream"),
        (protocol_packet({"error": "upstream-private-error"}, b""), "tts_unavailable"),
        (protocol_packet({"error": "tts_timeout"}, b""), "tts_timeout"),
        (
            protocol_packet(
                {"format": "mp3", "audio_bytes": 10 * 1024**2 + 1, "boundaries": []}, b""
            ),
            "tts_audio_too_large",
        ),
        (
            protocol_packet({"format": "mp3", "audio_bytes": 3, "boundaries": []}, b"ID3"),
            "speech_boundaries_unavailable",
        ),
        (b"broken", "tts_invalid_stream"),
        (protocol_packet(audio=b"not-mp3"), "tts_invalid_audio"),
    ],
)
def test_edge_adapter_rejects_invalid_worker_frames(packet, code, monkeypatch):
    killed = []

    async def factory(*args, **kwargs):
        return MockProcess(packet)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", factory)
    monkeypatch.setattr("app.tts.edge.os.killpg", lambda *args: killed.append(args))
    with pytest.raises(TTSError, match=code):
        run(EdgeTTS().synthesize("Hello", None, 1, "en", approved=True))


def test_cancelling_edge_adapter_kills_network_child_group(monkeypatch):
    killed = []
    entered = None

    async def scenario():
        nonlocal entered
        entered = asyncio.Event()

        async def factory(*args, **kwargs):
            process = MockProcess(hanging=True)
            entered.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", factory)
        task = asyncio.create_task(EdgeTTS().synthesize("Hello", None, 1, "en", approved=True))
        await entered.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr("app.tts.edge.os.killpg", lambda *args: killed.append(args))
    run(scenario())
    assert killed and killed[0][0] == 123456789


def test_subtitles_use_speech_times_and_exact_scene_offsets():
    scenes = [
        SubtitleScene(
            [SpeechBoundary(0.2, 0.6, "Hello"), SpeechBoundary(0.8, 1.3, "world")], 5, 1.4
        ),
        SubtitleScene([], 3),
        SubtitleScene(
            [SpeechBoundary(0.1, 0.9, "欢迎"), SpeechBoundary(1, 1.5, "来到云端")], 4, 1.6
        ),
    ]
    cues = build_subtitles(scenes, opening_offset_seconds=2)
    assert cues == [SubtitleCue(2.2, 3.3, "Hello world"), SubtitleCue(10.1, 11.5, "欢迎来到云端")]
    assert (
        to_srt(cues)
        == "1\n00:00:02,200 --> 00:00:03,300\nHello world\n\n2\n00:00:10,100 --> 00:00:11,500\n欢迎来到云端\n\n"
    )


def test_subtitle_grouping_does_not_rescale_timestamps_by_character_count():
    boundaries = [
        SpeechBoundary(0.1, 0.5, "Small"),
        SpeechBoundary(0.6, 1.2, "description"),
        SpeechBoundary(3, 3.4, "later"),
    ]
    grouped = build_subtitles([SubtitleScene(boundaries, 5, 3.5)], max_characters=10)
    assert [cue.start_seconds for cue in grouped] == [0.1, 0.6, 3]
    assert [cue.end_seconds for cue in grouped] == [0.5, 1.2, 3.4]


def test_caption_controls_cannot_become_srt_html_or_ass_overrides():
    original = r"<font color='red'>{\pos(0,0)}hello\Nworld &lt;i&gt;" + "\x00\n\u202e"
    safe = safe_subtitle_text(original)
    assert (
        "<" not in safe
        and ">" not in safe
        and "{" not in safe
        and "\\" not in safe
        and "&" not in safe
    )
    assert "\x00" not in safe and "\n" not in safe and "\u202e" not in safe
    assert "hello" in safe and "world" in safe
    body = to_srt([SubtitleCue(0, 1, original)])
    assert body.count(" --> ") == 1 and body.count("\n\n") == 1


@pytest.mark.parametrize(
    "boundaries,scene,audio,code",
    [
        ([], 5, 1, "speech_boundaries_unavailable"),
        ([SpeechBoundary(0, 5.1, "Long")], 5, 5.1, "narration_exceeds_scene_duration"),
        ([SpeechBoundary(0, 4, "Late")], 5, 3, "speech_boundaries_exceed_audio"),
        ([SpeechBoundary(0, 1, "Hello")], 5, 6, "narration_exceeds_scene_duration"),
        ([SpeechBoundary(1, 0, "Backward")], 5, 1, "invalid_speech_boundaries"),
        ([SpeechBoundary(0, math.inf, "Infinite")], 5, 1, "invalid_speech_boundaries"),
    ],
)
def test_subtitle_timing_rejects_missing_or_inconsistent_alignment(boundaries, scene, audio, code):
    with pytest.raises(TTSError, match=code):
        validate_timing(boundaries, scene, audio)


def test_subtitle_output_is_bounded_and_monotonic():
    assert to_srt([]) == ""
    with pytest.raises(TTSError, match="invalid_subtitle_timing"):
        to_srt([SubtitleCue(1, 2, "First"), SubtitleCue(1.5, 3, "Overlap")])
    with pytest.raises(TTSError, match="invalid_subtitle_timing"):
        to_srt([SubtitleCue(0.00001, 0.00002, "Submillisecond")])
    with pytest.raises(TTSError, match="subtitle_timeline_too_large"):
        to_srt([SubtitleCue(i, i + 0.5, "x" * 5000) for i in range(210)])
    with pytest.raises(TTSError, match="subtitle_timeline_too_large"):
        build_subtitles([SubtitleScene([], 3600)] * 3)
    with pytest.raises(TTSError, match="invalid_speech_boundaries"):
        validate_boundaries([SpeechBoundary(10**400, 10**401, "Overflow")])


def test_actual_worker_rejects_input_before_importing_network_adapter():
    """Exercise the real child module/protocol without importing or calling Edge."""
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "app.tts.edge_worker", str(os.getpid())],
        input=json.dumps({**REQUEST, "text": ""}).encode(),
        capture_output=True,
        timeout=5,
        check=False,
        env={"PATH": os.defpath, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 2 and not result.stderr
    size = struct.unpack("!I", result.stdout[:4])[0]
    assert json.loads(result.stdout[4 : 4 + size]) == {"error": "invalid_narration_text"}
    assert len(result.stdout) == 4 + size


def test_linux_child_parent_death_configuration_fails_closed(monkeypatch):
    from app.tts import edge_worker

    calls = []

    class Libc:
        @staticmethod
        def prctl(*args):
            calls.append(args)
            return 0

    monkeypatch.setattr(edge_worker.sys, "platform", "linux")
    monkeypatch.setattr(edge_worker.ctypes, "CDLL", lambda *args, **kwargs: Libc())
    monkeypatch.setattr(edge_worker.os, "getppid", lambda: 456)
    edge_worker.install_parent_death_signal(456)
    assert calls == [(1, __import__("signal").SIGKILL, 0, 0, 0)]
    with pytest.raises(TTSError, match="tts_parent_unavailable"):
        edge_worker.install_parent_death_signal(999)
    with pytest.raises(TTSError, match="tts_parent_unavailable"):
        edge_worker.install_parent_death_signal(True)

    class FailedLibc:
        @staticmethod
        def prctl(*args):
            return -1

    monkeypatch.setattr(edge_worker.ctypes, "CDLL", lambda *args, **kwargs: FailedLibc())
    with pytest.raises(TTSError, match="tts_parent_unavailable"):
        edge_worker.install_parent_death_signal(456)


@pytest.mark.skipif(
    not __import__("sys").platform.startswith("linux"), reason="Linux parent-death signal"
)
def test_hard_killed_parent_does_not_leave_edge_child_running():
    import contextlib
    import os
    import select
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    # Import and exercise only the death guard, never the Edge network adapter.
    child_program = "from app.tts.edge_worker import install_parent_death_signal; import os,sys,time; install_parent_death_signal(int(sys.argv[1])); print(os.getpid(),flush=True); time.sleep(30)"
    parent_program = "import os,subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c',sys.argv[1],str(os.getpid())],stdout=subprocess.PIPE,text=True); print(child.stdout.readline().strip(),flush=True); time.sleep(30)"
    process = subprocess.Popen(
        [sys.executable, "-c", parent_program, child_program],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
        env={"PATH": os.defpath, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        assert select.select([process.stdout], [], [], 5)[0], "Guard child did not start"
        line = process.stdout.readline().strip()
        assert line.isdecimal()
        child_pid = int(line)
        process.kill()  # Kill only the parent, not its process group.
        process.wait(timeout=5)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = Path(f"/proc/{child_pid}/stat")
            try:
                state = status.read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("Edge guard child remained alive after parent SIGKILL")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        process.stdout.close()
