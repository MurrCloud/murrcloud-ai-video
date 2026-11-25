"""Real synthetic FFmpeg processing; no cloud generation or speech services."""

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from app.media import (
    CompositionRequest,
    MediaError,
    SceneMedia,
    compose,
    probe_media,
    validate_output,
)
from app.media.composer import _safe_subtitles
from app.media.process import ProcessRunner, checked_file
from PIL import Image


@pytest.fixture(scope="module")
def source_media(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg/FFprobe are not installed in this runtime")
    folder = tmp_path_factory.mktemp("real-media-source")
    for name, size, color in [("landscape", "640x360", "orange"), ("portrait", "360x640", "blue")]:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-n",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s={size}:r=30:d=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=1",
                "-t",
                "1",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-threads",
                "1",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(folder / f"{name}.mp4"),
            ],
            timeout=15,
            check=True,
            capture_output=True,
        )
    for name, duration in [("voice", 0.6), ("longvoice", 2), ("music", 0.4)]:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-n",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=660:sample_rate=48000:duration={duration}",
                "-ac",
                "2",
                str(folder / f"{name}.wav"),
            ],
            timeout=10,
            check=True,
            capture_output=True,
        )
    Image.new("RGBA", (300, 300), (255, 125, 0, 180)).save(folder / "logo.png")
    return folder


def request(tmp_path, source_media, **changes):
    return CompositionRequest(
        [SceneMedia(source_media / "landscape.mp4", 1, "cut")],
        tmp_path / "final.mp4",
        tmp_path,
        allow_upscale=True,
        **changes,
    )


@pytest.mark.parametrize(
    "aspect_ratio,dimensions",
    [("16:9", (1920, 1080)), ("9:16", (1080, 1920)), ("1:1", (1080, 1080))],
)
def test_real_default_exports_have_h264_aac_30fps_and_integrity(
    source_media, tmp_path, aspect_ratio, dimensions
):
    progress = []
    result = compose(
        request(tmp_path, source_media, aspect_ratio=aspect_ratio, progress=progress.append)
    )
    assert (result.width, result.height) == dimensions
    assert result.fps == 30 and abs(result.duration_seconds - 1) < 0.15
    assert result.report["video_codec"] == "h264" and result.report["audio_codec"] == "aac"
    assert result.report["integrity_decoded"] is True
    assert progress[0] == 0 and progress[-1] == 100
    assert sorted(path.name for path in tmp_path.iterdir()) == ["final.mp4"]


def test_upscale_requires_explicit_acknowledgement_before_encoding(source_media, tmp_path):
    with pytest.raises(MediaError, match="source_upscale_approval_required"):
        compose(replace(request(tmp_path, source_media), allow_upscale=False))
    assert list(tmp_path.iterdir()) == []
    result = compose(request(tmp_path, source_media))
    assert result.warnings == ["scene_1_output_exceeds_source_resolution"]
    assert result.report["upscale_acknowledged"] is True


def test_real_dissolve_voice_music_and_watermark_preserve_scene_timeline(source_media, tmp_path):
    scenes = [
        SceneMedia(source_media / "landscape.mp4", 1, "dissolve", source_media / "voice.wav"),
        SceneMedia(source_media / "portrait.mp4", 1, "cut"),
    ]
    result = compose(
        replace(
            request(tmp_path, source_media),
            scenes=scenes,
            music_path=source_media / "music.wav",
            watermark_path=source_media / "logo.png",
            source_audio_volume=0.1,
        )
    )
    assert abs(result.duration_seconds - 2) < 0.15
    assert result.report["expected_duration_seconds"] == 2
    assert result.report["transition_window_seconds"] == 0.4
    assert len(result.report["source_media"]) == 2


def test_real_fade_does_not_shorten_scenes(source_media, tmp_path):
    scenes = [
        SceneMedia(source_media / "landscape.mp4", 1, "fade"),
        SceneMedia(source_media / "portrait.mp4", 1, "cut"),
    ]
    result = compose(replace(request(tmp_path, source_media), scenes=scenes))
    assert abs(result.duration_seconds - 2) < 0.15


def test_long_narration_is_rejected_without_truncating_words(source_media, tmp_path):
    with pytest.raises(MediaError, match="narration_exceeds_scene_duration"):
        compose(
            replace(
                request(tmp_path, source_media),
                scenes=[
                    SceneMedia(
                        source_media / "landscape.mp4",
                        1,
                        narration_path=source_media / "longvoice.wav",
                    )
                ],
            )
        )
    assert list(tmp_path.iterdir()) == []


def test_real_titles_and_burned_chinese_speech_subtitles(source_media, tmp_path):
    filters = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, timeout=10, check=True
    ).stdout
    if b" subtitles " not in filters or b" drawtext " not in filters:
        pytest.skip("Developer FFmpeg lacks libass/drawtext; Debian CI image provides both")
    if not Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc").is_file():
        pytest.skip("CJK font is unavailable locally; Debian CI installs fonts-noto-cjk")
    srt = source_media / "narration.srt"
    srt.write_text("1\n00:00:02,000 --> 00:00:02,600\n中文旁白。English words.\n", encoding="utf-8")
    result = compose(
        request(
            tmp_path,
            source_media,
            opening_title="MurrCloud 开场 %{not_an_expression}",
            closing_title="感谢观看",
            subtitles_path=srt,
            burn_subtitles=True,
        )
    )
    assert abs(result.duration_seconds - 5) < 0.15
    assert result.report["opening_title_seconds"] == result.report["closing_title_seconds"] == 2


def test_subtitle_markup_is_literal_and_times_are_bounded(tmp_path):
    source, destination = tmp_path / "speech.srt", tmp_path / "safe.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:00,600\n{\\p1}<font>中文</font>\n", encoding="utf-8"
    )
    _safe_subtitles(source, destination, 1)
    assert "｛＼p1｝＜font＞中文＜/font＞" in destination.read_text()
    source.write_text("1\n00:00:00,000 --> 00:00:02,600\nToo late\n", encoding="utf-8")
    with pytest.raises(MediaError, match="invalid_subtitle_timing"):
        _safe_subtitles(source, destination, 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"aspect_ratio": "4:3"},
        {"ffmpeg_threads": 2},
        {"music_volume": float("nan")},
        {"scenes": []},
        {"opening_title": "x" * 201},
        {"opening_title": "bad\x00title"},
        {"scenes": [SceneMedia(Path("none"), 0.3)]},
    ],
)
def test_invalid_settings_fail_closed_before_processing(source_media, tmp_path, changes):
    with pytest.raises(
        MediaError, match="invalid_composition_settings|invalid_title_text|invalid_scene_timing"
    ):
        compose(replace(request(tmp_path, source_media), **changes))


def test_protocol_disguises_symlinks_and_output_traversal_are_rejected(source_media, tmp_path):
    fake = tmp_path / "input.mp4"
    fake.write_text("#EXTM3U\nhttp://169.254.169.254/latest/meta-data\n")
    with pytest.raises(MediaError, match="media_container_rejected"):
        probe_media(fake)
    fake.unlink()
    fake.symlink_to(source_media / "landscape.mp4")
    with pytest.raises(MediaError, match="unsafe_media_path"):
        checked_file(fake)
    with pytest.raises(MediaError, match="unsafe_output_path"):
        compose(replace(request(tmp_path, source_media), output=tmp_path.parent / "outside.mp4"))


def test_scratch_quota_and_host_admission_fail_without_publishing(source_media, tmp_path):
    with pytest.raises(MediaError, match="scratch_quota_exceeded|media_processing_failed"):
        compose(replace(request(tmp_path, source_media), max_scratch_bytes=1024))
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(MediaError, match="host_admission_refused"):
        compose(request(tmp_path, source_media, admission=lambda: False))
    assert list(tmp_path.iterdir()) == []


def test_full_decode_rejects_truncated_mp4(source_media, tmp_path):
    source = source_media / "landscape.mp4"
    broken = tmp_path / "broken.mp4"
    content = source.read_bytes()
    broken.write_bytes(content[:-1000])
    with pytest.raises(MediaError):
        validate_output(broken, 1, 640, 360)


def test_process_deadline_kills_the_process_group_and_removes_secrets(monkeypatch):
    monkeypatch.setenv("VIDEO_FAL_KEY", "must-not-reach-media-child")
    runner = ProcessRunner(timeout_seconds=10)
    output = runner.run(
        [sys.executable, "-c", "import os; print(os.environ.get('VIDEO_FAL_KEY','absent'))"]
    )
    assert output.strip() == b"absent"
    started = time.monotonic()
    with pytest.raises(MediaError, match="media_processing_timeout"):
        ProcessRunner(timeout_seconds=0.3).run(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
    assert time.monotonic() - started < 3


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux PR_SET_PDEATHSIG contract")
def test_child_dies_when_its_worker_parent_is_hard_killed(tmp_path):
    program = """import os,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-m','app.media.child',str(os.getpid()),'402653184','536870912','/bin/sleep','30'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(child.pid,flush=True)
time.sleep(30)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", program], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    child_pid = int(parent.stdout.readline())
    try:
        time.sleep(0.2)
        parent.kill()
        parent.wait(timeout=2)
        for _ in range(30):
            try:
                state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
                if state == "Z":
                    break
            except FileNotFoundError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("Media child survived worker SIGKILL")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=2)
        parent.stdout.close()
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_narration_starts_at_scene_zero_and_short_speech_has_a_silent_tail(source_media, tmp_path):
    import array
    import math

    result = compose(
        replace(
            request(tmp_path, source_media),
            scenes=[
                SceneMedia(
                    source_media / "landscape.mp4", 1, narration_path=source_media / "voice.wav"
                )
            ],
        )
    )
    pcm = ProcessRunner(timeout_seconds=10).run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-i",
            str(result.path),
            "-map",
            "0:a:0",
            "-t",
            "1",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-f",
            "s16le",
            "-",
        ]
    )
    samples = array.array("h", pcm)

    def rms(start, end):
        window = samples[int(start * 16000) : int(end * 16000)]
        return math.sqrt(sum(sample * sample for sample in window) / len(window))

    assert rms(0.08, 0.2) > 100
    assert rms(0.85, 0.95) < 2


def test_resource_failure_diagnostic_never_exposes_raw_media_or_secrets():
    with pytest.raises(MediaError) as failure:
        ProcessRunner(timeout_seconds=10).run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('Cannot allocate memory: private-path-and-secret'); sys.exit(1)",
            ]
        )
    assert failure.value.code == "media_memory_limit"
    assert str(failure.value) == "media_memory_limit"
    assert "private-path" not in str(failure.value)
