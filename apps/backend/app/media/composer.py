"""Sequential, bounded FFmpeg composition with duration-preserving transitions."""

import math
import re
import shutil
import tempfile
import textwrap
from pathlib import Path

from app.media.contracts import CompositionRequest, CompositionResult, MediaError
from app.media.probe import input_args, input_format, probe_media, validate_output
from app.media.process import ProcessRunner, checked_file

DIMENSIONS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
HALF_TRANSITION = 0.2  # six frames at 30 FPS, so subtitle timing cannot drift.
FPS = 30


def _base():
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-xerror",
        "-max_alloc",
        "67108864",
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-n",
    ]


def _encode(duration):
    return [
        "-t",
        f"{duration:.6f}",
        "-r",
        str(FPS),
        "-c:v",
        "libx264",
        "-threads",
        "1",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-refs",
        "1",
        "-bf",
        "0",
        "-crf",
        "23",
        "-maxrate",
        "4000k",
        "-bufsize",
        "8000k",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-sn",
        "-dn",
        "-video_track_timescale",
        "90000",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
    ]


def _run(runner, args, output, work, duration, progress=None):
    def report(seconds):
        if progress is not None:
            progress(min(1, seconds / duration) if duration else 0)

    runner.run(
        [*_base(), *args, *_encode(duration), "-progress", "pipe:1", str(output)],
        cwd=work,
        progress=report,
    )


def _number(value, minimum, maximum):
    return type(value) in {float, int} and math.isfinite(value) and minimum <= value <= maximum


def _validate_request(request):
    if (
        request.aspect_ratio not in DIMENSIONS
        or not 1 <= len(request.scenes) <= 120
        or request.ffmpeg_threads != 1
        or not _number(request.timeout_seconds, 1, 7200)
        or not _number(request.music_volume, 0, 1)
        or not _number(request.source_audio_volume, 0, 1)
        or not _number(request.title_duration_seconds, 1, 10)
        or type(request.max_output_bytes) is not int
        or request.max_output_bytes < 1024
        or type(request.max_scratch_bytes) is not int
        or request.max_scratch_bytes < 1024
    ):
        raise MediaError("invalid_composition_settings")
    for title in (request.opening_title, request.closing_title):
        if (
            not isinstance(title, str)
            or len(title) > 200
            or any(ord(c) < 32 and c != "\n" for c in title)
        ):
            raise MediaError("invalid_title_text")
    for scene in request.scenes:
        if (
            not _number(scene.duration_seconds, 1, 30)
            or abs(scene.duration_seconds * FPS - round(scene.duration_seconds * FPS)) > 0.001
            or scene.transition not in {"cut", "fade", "dissolve"}
        ):
            raise MediaError("invalid_scene_timing")
    total = sum(scene.duration_seconds for scene in request.scenes)
    total += request.title_duration_seconds * sum(
        bool(title.strip()) for title in (request.opening_title, request.closing_title)
    )
    if total > 3600:
        raise MediaError("composition_duration_limit")
    scratch = Path(request.scratch).absolute()
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    if scratch.resolve() != scratch or not scratch.is_dir():
        raise MediaError("unsafe_scratch_path")
    output = Path(request.output).absolute()
    if output.parent != scratch or not re.fullmatch(r"[A-Za-z0-9_-]+\.mp4", output.name):
        raise MediaError("unsafe_output_path")
    if output.exists() or output.is_symlink():
        raise MediaError("render_output_exists")
    return scratch, output, total


def _normalise(scene, index, scenes, work, width, height, request, runner, progress):
    path = checked_file(scene.path)
    if input_format(path) != "mov":
        raise MediaError("scene_mp4_required")
    source = probe_media(path, runner=runner)
    if not source.has_video or source.duration_seconds + 0.15 < scene.duration_seconds:
        raise MediaError("source_clip_too_short")
    arguments = input_args(path)
    # A known bounded silence source fixes exact audio duration, even for silent
    # source clips; native and narration audio are mixed against this timeline.
    arguments += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    filters = [
        f"[0:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease:flags=bicubic,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={FPS},"
        f"tpad=stop_mode=clone:stop_duration=0.15,trim=duration={scene.duration_seconds},"
        "setpts=PTS-STARTPTS,format=yuv420p[v0]"
    ]
    video = "v0"
    if index > 0 and scenes[index - 1].transition == "fade":
        filters += [f"[{video}]fade=t=in:st=0:d={HALF_TRANSITION}[vin]"]
        video = "vin"
    if index < len(scenes) - 1 and scene.transition == "fade":
        filters += [
            f"[{video}]fade=t=out:st={scene.duration_seconds - HALF_TRANSITION}:d={HALF_TRANSITION}[vout]"
        ]
        video = "vout"
    filters += [f"[1:a:0]atrim=duration={scene.duration_seconds},asetpts=PTS-STARTPTS[a0]"]
    audio_labels = ["[a0]"]
    if source.has_audio and request.source_audio_volume > 0:
        filters += [
            f"[0:a:0]atrim=duration={scene.duration_seconds},asetpts=PTS-STARTPTS,"
            f"aresample=48000,volume={request.source_audio_volume}[native]"
        ]
        audio_labels += ["[native]"]
    if scene.narration_path is not None:
        narration = probe_media(scene.narration_path, runner=runner)
        if not narration.has_audio or narration.duration_seconds <= 0:
            raise MediaError("invalid_narration_audio")
        if narration.duration_seconds > scene.duration_seconds + 0.05:
            raise MediaError("narration_exceeds_scene_duration")
        arguments += input_args(scene.narration_path)
        filters += [
            f"[2:a:0]atrim=duration={scene.duration_seconds},asetpts=PTS-STARTPTS,"
            "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[voice]"
        ]
        audio_labels += ["[voice]"]
    filters += [
        "".join(audio_labels) + f"amix=inputs={len(audio_labels)}:duration=first:"
        "dropout_transition=0:normalize=0,alimiter=limit=0.95:level=0,"
        f"apad,atrim=duration={scene.duration_seconds}[audio]"
    ]
    arguments += ["-filter_complex", ";".join(filters), "-map", f"[{video}]", "-map", "[audio]"]
    result = work / f"normal-{index:03}.mp4"
    _run(runner, arguments, result, work, scene.duration_seconds, progress)
    return result, source


def _trim_part(source, start, end, output, work, runner):
    duration = end - start
    filters = (
        f"[0:v:0]trim=start={start}:end={end},setpts=PTS-STARTPTS[v];"
        f"[0:a:0]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a]"
    )
    _run(
        runner,
        [*input_args(source), "-filter_complex", filters, "-map", "[v]", "-map", "[a]"],
        output,
        work,
        duration,
    )


def _dissolve(left, right, left_duration, output, work, runner):
    half = HALF_TRANSITION
    # Clone one endpoint frame on each side: the resulting 0.4s visual window
    # replaces exactly 0.2s from each core. Audio is concatenated, never overlapped.
    filters = (
        f"[0:v:0]trim=start={left_duration - half}:end={left_duration},setpts=PTS-STARTPTS,"
        f"tpad=stop_mode=clone:stop_duration={half},settb=AVTB[l];"
        f"[1:v:0]trim=start=0:end={half},setpts=PTS-STARTPTS,"
        f"tpad=start_mode=clone:start_duration={half},settb=AVTB[r];"
        f"[l][r]xfade=transition=fade:duration={half * 2}:offset=0,format=yuv420p[v];"
        f"[0:a:0]atrim=start={left_duration - half}:end={left_duration},asetpts=PTS-STARTPTS[la];"
        f"[1:a:0]atrim=start=0:end={half},asetpts=PTS-STARTPTS[ra];"
        "[la][ra]concat=n=2:v=0:a=1[a]"
    )
    _run(
        runner,
        [
            *input_args(left),
            *input_args(right),
            "-filter_complex",
            filters,
            "-map",
            "[v]",
            "-map",
            "[a]",
        ],
        output,
        work,
        half * 2,
    )


def _font():
    choices = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in choices:
        if Path(candidate).is_file():
            return candidate
    raise MediaError("render_font_unavailable")


def _title_card(text, label, duration, work, width, height, runner):
    textfile = work / f"{label}.txt"
    textfile.write_text(
        "\n".join(
            textwrap.wrap(text.strip(), width=max(12, int(width / 55)), replace_whitespace=False)
        ),
        encoding="utf-8",
    )
    # User text is read only from the generated file. expansion=none prevents
    # %{...} expressions from being evaluated as drawtext commands.
    video = (
        f"color=c=0x161616:s={width}x{height}:r={FPS}:d={duration},"
        "drawbox=x=0:y=0:w=iw:h=8:color=0xFF7D00:t=fill,"
        f"drawtext=fontfile='{_font()}':textfile={textfile.name}:expansion=none:"
        "fontsize=48:fontcolor=white:line_spacing=12:x=(w-text_w)/2:y=(h-text_h)/2"
    )
    output = work / f"{label}.mp4"
    _run(
        runner,
        [
            "-f",
            "lavfi",
            "-i",
            video,
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ],
        output,
        work,
        duration,
    )
    return output


def _safe_subtitles(source, destination, expected_duration):
    source = checked_file(source, 1024**2)
    try:
        data = source.read_text(encoding="utf-8-sig")
    except UnicodeError:
        raise MediaError("invalid_subtitle_file") from None
    if "\x00" in data:
        raise MediaError("invalid_subtitle_file")
    previous, cues = 0, []
    for block in re.split(r"\n\s*\n", data.replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].isdigit():
            raise MediaError("invalid_subtitle_file")
        match = re.fullmatch(
            r"(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})", lines[1]
        )
        if not match:
            raise MediaError("invalid_subtitle_file")
        parts = list(map(int, match.groups()))

        def seconds(values):
            h, m, s, ms = values
            if h > 23 or m > 59 or s > 59:
                raise MediaError("invalid_subtitle_file")
            return h * 3600 + m * 60 + s + ms / 1000

        start, end = seconds(parts[:4]), seconds(parts[4:])
        if start < previous or not start < end <= expected_duration + 0.05:
            raise MediaError("invalid_subtitle_timing")
        text = "\n".join(lines[2:])
        if not text or len(text) > 2000 or len(lines) > 8 or len(cues) >= 10_000:
            raise MediaError("subtitle_text_limit")
        # Neither ASS override tags nor SRT HTML styling are interpreted.
        text = text.translate(
            str.maketrans({"{": "｛", "}": "｝", "\\": "＼", "<": "＜", ">": "＞"})
        )
        cues.append(f"{len(cues) + 1}\n{lines[1]}\n{text}")
        previous = start
    destination.write_text("\n\n".join(cues) + "\n", encoding="utf-8")


def _finalize(parts, output, work, duration, width, height, request, runner, progress):
    listing = work / "concat.txt"
    # Only our fixed generated basenames enter this concat script, never uploads.
    listing.write_text("\n".join(f"file '{p.name}'" for p in parts) + "\n", encoding="ascii")
    arguments = [
        "-protocol_whitelist",
        "file,pipe",
        "-f",
        "concat",
        "-safe",
        "1",
        "-auto_convert",
        "0",
        "-i",
        str(listing),
    ]
    filters, video_label, audio_label, next_input = [], "0:v:0", "0:a:0", 1
    if request.watermark_path is not None:
        watermark = checked_file(request.watermark_path, 20 * 1024**2)
        if input_format(watermark) not in {"png_pipe", "jpeg_pipe"}:
            raise MediaError("invalid_watermark")
        from PIL import Image

        try:
            with Image.open(watermark, formats=["JPEG", "PNG"]) as image:
                if image.width * image.height > 8_388_608 or getattr(image, "n_frames", 1) != 1:
                    raise MediaError("invalid_watermark")
                image.verify()
        except (OSError, ValueError):
            raise MediaError("invalid_watermark") from None
        arguments += [
            "-protocol_whitelist",
            "file,pipe",
            "-loop",
            "1",
            "-framerate",
            "30",
            "-f",
            "image2",
            "-pattern_type",
            "none",
            "-i",
            str(watermark),
        ]
        filters += [
            f"[{next_input}:v:0]scale={width // 5}:{height // 5}:force_original_aspect_ratio=decrease[logo]",
            "[0:v:0][logo]overlay=x=W-w-24:y=H-h-24:shortest=1[vlogo]",
        ]
        video_label = "vlogo"
        next_input += 1
    if request.burn_subtitles:
        if request.subtitles_path is None:
            raise MediaError("subtitles_required")
        _safe_subtitles(request.subtitles_path, work / "subtitles.srt", duration)
        filters += [
            f"[{video_label}]subtitles=filename=subtitles.srt:"
            "force_style='FontName=Noto Sans CJK SC,FontSize=20,Outline=1,MarginV=24'[vsub]"
        ]
        video_label = "vsub"
    if request.music_path is not None:
        music = probe_media(request.music_path, runner=runner)
        if not music.has_audio or not 0 < music.duration_seconds <= 1800:
            raise MediaError("invalid_background_music")
        arguments += input_args(request.music_path, loop=True)
        filters += [
            f"[{next_input}:a:0]atrim=duration={duration},asetpts=PTS-STARTPTS,"
            f"aresample=48000,volume={request.music_volume}[music]",
            f"[0:a:0][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            f"alimiter=limit=0.95:level=0,atrim=duration={duration}[mixed]",
        ]
        audio_label = "mixed"
    if filters:
        arguments += ["-filter_complex", ";".join(filters)]
    arguments += [
        "-map",
        f"[{video_label}]" if video_label != "0:v:0" else video_label,
        "-map",
        f"[{audio_label}]" if audio_label != "0:a:0" else audio_label,
    ]
    _run(runner, arguments, output, work, duration, progress)


def compose(request: CompositionRequest) -> CompositionResult:
    scratch, output, duration = _validate_request(request)
    width, height = DIMENSIONS[request.aspect_ratio]
    runner = ProcessRunner(
        timeout_seconds=request.timeout_seconds,
        scratch=scratch,
        max_scratch_bytes=request.max_scratch_bytes,
        output=output,
        max_output_bytes=request.max_output_bytes,
        admission=request.admission,
    )
    warnings, sources = [], []
    work = Path(tempfile.mkdtemp(prefix="compose-", dir=scratch))
    completed = False

    def progress(percent):
        if request.progress is not None:
            request.progress(min(100, max(0, int(percent))))

    try:
        progress(0)
        # Probe all source dimensions before performing any expensive processing.
        for index, scene in enumerate(request.scenes):
            source = probe_media(scene.path, runner=runner)
            if not source.has_video:
                raise MediaError("scene_video_required")
            factor = min(width / source.width, height / source.height)
            if factor > 1.001:
                if not request.allow_upscale:
                    raise MediaError("source_upscale_approval_required")
                warnings.append(f"scene_{index + 1}_output_exceeds_source_resolution")
            sources.append(source.report)
        normalized = []
        for index, scene in enumerate(request.scenes):
            path, _ = _normalise(
                scene,
                index,
                request.scenes,
                work,
                width,
                height,
                request,
                runner,
                lambda fraction, index=index: progress(
                    5 + 45 * (index + fraction) / len(request.scenes)
                ),
            )
            normalized.append(path)
        parts = []
        if request.opening_title.strip():
            parts.append(
                _title_card(
                    request.opening_title,
                    "opening",
                    request.title_duration_seconds,
                    work,
                    width,
                    height,
                    runner,
                )
            )
        for index, scene in enumerate(request.scenes):
            before = index > 0 and request.scenes[index - 1].transition == "dissolve"
            after = index < len(request.scenes) - 1 and scene.transition == "dissolve"
            if before or after:
                piece = work / f"core-{index:03}.mp4"
                _trim_part(
                    normalized[index],
                    HALF_TRANSITION if before else 0,
                    scene.duration_seconds - (HALF_TRANSITION if after else 0),
                    piece,
                    work,
                    runner,
                )
                parts.append(piece)
            else:
                parts.append(normalized[index])
            if after:
                boundary = work / f"blend-{index:03}.mp4"
                _dissolve(
                    normalized[index],
                    normalized[index + 1],
                    scene.duration_seconds,
                    boundary,
                    work,
                    runner,
                )
                parts.append(boundary)
            progress(50 + 15 * (index + 1) / len(request.scenes))
        if request.closing_title.strip():
            parts.append(
                _title_card(
                    request.closing_title,
                    "closing",
                    request.title_duration_seconds,
                    work,
                    width,
                    height,
                    runner,
                )
            )
        # Normalized whole clips that were split are no longer needed by final
        # composition; release them before writing the output in bounded scratch.
        for path in normalized:
            if path not in parts:
                path.unlink()
        _finalize(
            parts,
            output,
            work,
            duration,
            width,
            height,
            request,
            runner,
            lambda fraction: progress(65 + 28 * fraction),
        )
        progress(94)
        checked = validate_output(output, duration, width, height, runner=runner)
        progress(100)
        completed = True
        return CompositionResult(
            output,
            checked.duration_seconds,
            width,
            height,
            FPS,
            warnings,
            {
                **checked.report,
                "fps": checked.fps,
                "integrity_decoded": True,
                "expected_duration_seconds": duration,
                "source_media": sources,
                "upscale_acknowledged": bool(request.allow_upscale),
                "transition_window_seconds": HALF_TRANSITION * 2,
                "opening_title_seconds": request.title_duration_seconds
                if request.opening_title.strip()
                else 0,
                "closing_title_seconds": request.title_duration_seconds
                if request.closing_title.strip()
                else 0,
            },
        )
    finally:
        # The path was freshly generated inside the already verified private
        # directory; uploaded names or URLs can never select the cleanup target.
        if work.parent == scratch and work.resolve() == work:
            shutil.rmtree(work)
        if not completed and output.exists() and not output.is_symlink():
            output.unlink()
