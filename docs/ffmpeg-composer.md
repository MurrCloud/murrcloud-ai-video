# FFmpeg compositor and validation contract

Implementation reference review: 2026-11-06. Official FFmpeg search-index excerpts
were available; direct documentation fetches timed out. Runtime help and real
synthetic media processing additionally verify the options used. No cloud AI or
speech service is contacted by these tests.

## Public interface

`app.media` exports `SceneMedia`, `CompositionRequest`, `CompositionResult`,
`ProbeResult`, `MediaError`, `compose`, `probe_media` and `validate_output`.

A scene specifies a private local MP4 path, duration, outgoing transition and
optional local narration path. Composition receives a fresh private scratch
folder and a new direct-child `.mp4` output path. Optional inputs are global
speech-timed SRT, music, canonical PNG/JPEG watermark, opening/closing text,
upscale acknowledgement, and bounded volumes/deadline/storage limits.

Progress receives integer percentages. Admission returns a boolean and is checked
before tools and during processing; false pauses work by raising a retryable
`host_admission_refused`. Callback exceptions propagate after process termination,
allowing orchestration cancellation/lease loss to stop work. Use a fresh scratch
folder per attempt; a pre-existing output is never overwritten.

The result contains the final path, measured duration/dimensions/FPS, visible
upscale warnings and a validation report. It does not publish an asset or mark a
project complete; the orchestrator must perform its snapshot/ownership/quota checks
and atomic publication afterwards. No provider API schema enters this module.

## Timeline and media behavior

All output uses 30 FPS, H.264/yuv420p and stereo 48 kHz AAC. The supported dimensions
are 1920x1080, 1080x1920 and 1080x1080. Footage is fitted and letterboxed, preserving
its display aspect ratio. Actual upward footage scaling is rejected unless
`allow_upscale` is explicit; affected scene numbers remain in output warnings.

Scene durations remain unchanged by transitions. A fade uses 0.2 seconds on each
side of a boundary. A dissolve replaces the last six frames and first six frames
with a 0.4-second blend of cloned endpoint handles. The voice track is concatenated
through that window without overlap. This avoids accumulating transition-induced
subtitle drift. Cuts join immediately. Opening and closing cards each add their
configured duration (two seconds by default), which is reported separately; global
SRT timing must already include the opening-card offset.

Narration must fit the scene, with a 50 ms decoding tolerance. Longer narration
fails with `narration_exceeds_scene_duration`; words are not silently clipped or
sped up. Short narration is padded with silence. Native audio is optional and
explicitly mixed at its configured volume. Narration receives EBU-style loudness
normalization and peak limiting. Optional music loops under the complete timeline
with bounded volume and a final limiter. These choices use FFmpeg's documented
[audio filters](https://ffmpeg.org/ffmpeg-filters.html).

Titles use generated UTF-8 files and `expansion=none`, avoiding evaluation of user
text as drawtext expressions. Debian rendering images require Noto CJK and DejaVu
fonts. Subtitle burning requires libass. ASS override braces/backslashes and SRT
HTML markers are converted to literal full-width characters before burning;
subtitles are validated for bounded UTF-8 size, cue count, text and timeline.
Filter support follows the official [drawtext, subtitles, concat and xfade
reference](https://ffmpeg.org/ffmpeg-filters.html).

## Resource and trust boundaries

Scenes are normalized one at a time. Dissolves process only two short endpoint
windows instead of decoding every scene concurrently. No shell is used. Uploaded
filenames, URLs and text never select filter commands or concat-list entries.
Only generated basenames enter the concat script. Known input containers are
explicitly selected after signature checks; playlists and other containers are
rejected. File/pipe are the only permitted protocols, and MOV external data
references are explicitly disabled. See the official [protocol allowlist
reference](https://ffmpeg.org/ffmpeg-protocols.html).

Every FFmpeg and FFprobe process runs through a small single-threaded exec wrapper.
It inherits no application credentials, uses one codec/filter thread, bounded
file descriptors and file size, and a Linux 768 MiB virtual address-space ceiling.
Virtual mappings include codec libraries and allocator arenas; this does not raise
the render container’s existing 512 MiB cgroup memory ceiling. Allocator arenas and H.264 references/B-frames
are explicitly bounded. Known allocation failures receive a safe diagnostic code
without exposing tool stderr. Linux
`PR_SET_PDEATHSIG` plus a parent-PID check kills it if its Celery worker disappears.
Normal cancellation/timeouts kill the entire process group. A process watchdog
checks the absolute deadline, output size, all scratch bytes, symlinks and admission
while draining bounded stdout/stderr; raw tool errors never reach API responses.

Default scratch is 128 MiB and output is capped at 512 MiB, with the scratch cap
also applying to output while it is staged there. Content that cannot fit fails
closed; these ceilings are not promises that arbitrary long or high-complexity
media can render on the assessed VPS. No resource allocation is increased here.
The orchestrator must hold its shared single-render lock and existing storage
quota/admission controls. Scratch cleanup removes only this invocation's generated
subdirectory and failed output.

## Validation and evidence

FFprobe returns bounded JSON fields for duration, resolution, frame rate, codecs,
stream durations and audio format. A finished output must have H.264 video, AAC
audio, exact configured dimensions, 30 FPS and expected duration within 150 ms.
A full error-strict FFmpeg decode then checks all selected video/audio frames;
a valid MP4 header alone does not establish integrity. This uses the official
[FFprobe structured output interface](https://ffmpeg.org/ffprobe.html).

Run `pytest -q tests/test_composition.py`. Tests actually generate and render
synthetic files for all aspect ratios, cut/fade/dissolve timing, narration/music/
watermark mixing, loudness/output streams, upscaling acknowledgement and truncated
file rejection. Deadline, environment, quota, path/protocol and Linux worker-death
regressions exercise processing boundaries. Local macOS FFmpeg lacks libass and
drawtext, so the Chinese title/burn test runs in the Debian CI image; Linux
parent-death testing also requires CI. Do not report those skipped cases as local
passes or infer paid cloud-video quality/protected-workload capacity from synthetic tests.
