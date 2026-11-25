# Narration and speech-aligned subtitles

Phase 4 provides a replaceable `TTSProvider` contract in `app.tts`. An adapter
returns MP3 audio and `SpeechBoundary(start_seconds, end_seconds, text)` records.
The initial `EdgeTTS` adapter is for approved internal evaluation. Its default
`approved=False` prevents a child process or speech request from starting. Worker
configuration and user authorization must independently permit narration before
calling with `approved=True`. No speech request was made during implementation.

## Upstream contract verified on 2026-11-06

The locked integration target is **edge-tts 7.3.1**, published on PyPI on
2026-10-13. It is an unofficial client for Microsoft's online Edge speech service;
it is not an Azure Speech SDK or a contracted speech service.

The upstream public `Communicate` API accepts a signed percentage rate and a
boundary setting. This adapter explicitly requests `WordBoundary` rather than
relying on the upstream sentence-boundary default. Stream audio is MP3, 24 kHz
mono at 48 kbit/s. Word metadata offsets and durations use 10,000,000 ticks per
second; the adapter converts those supplied timings to seconds. The library
handles its own inter-chunk timing compensation. It may perform its own 403
handshake retry. The application adds no request retry or alternative provider
call inside the adapter.

References: [PyPI release](https://pypi.org/project/edge-tts/7.3.1/),
[upstream API/source](https://github.com/rany2/edge-tts/blob/7.3.1/src/edge_tts/communicate.py),
[upstream chunk types](https://github.com/rany2/edge-tts/blob/7.3.1/src/edge_tts/typing.py),
[upstream usage](https://github.com/rany2/edge-tts/blob/7.3.1/README.md).

## Bounds and secrets

Input is plain text, at most 5,000 characters; arbitrary SSML is not accepted.
Voice and language must match the narrow English (`en`) or Simplified Chinese
(`zh-CN`) evaluation catalog. Rate is 0.50–2.00 in 0.01 increments, translated to
-50% through +100%. The catalog exposes Aria/Guy and Xiaoxiao/Yunxi as known
candidate identifiers, with `availability=unverified_live`. These names have
upstream/Microsoft documentation evidence, but no live voice availability or
quality is asserted. The deployment operator must verify the chosen Edge voice
before enabling it. Azure voice documentation alone does not prove Edge access.

Upstream uses environment-aware HTTP sessions. The integration therefore runs it
in a separate process with a minimal environment: no studio API keys, HTTP proxy
variables or application database credentials, and an explicit nonexistent NETRC
file. Credentials never enter speech requests. The child has bounded CPU, file
descriptors and Linux address space; the parent caps protocol headers at 1 MiB,
audio at 10 MiB, and the total operation at the configured deadline plus three
seconds of process overhead. On cancellation/timeout, the parent kills the child
process group. On Linux, a parent-death signal also kills the child immediately
if a hard-killed Celery parent cannot run cleanup; startup verifies the expected
parent PID before any Edge import. Raw exceptions, scripts and provider responses
are never surfaced.
This isolates configuration and resource use; it is not a claim of a hardened
OS sandbox against a compromised dependency.

Candidate documentation: [Microsoft voice names](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=tts),
[upstream Chinese voice discussion](https://github.com/rany2/edge-tts/issues/14).

## Subtitle timing and composition

`validate_timing` rejects missing, nonfinite, backward or overlapping speech
boundaries, boundaries beyond probed audio, and narration longer than its scene.
The pipeline must probe the actual audio and call this validator before rendering.
It must not silently truncate narration or substitute character-count timing.

`build_subtitles` groups words into short readable cues using their existing start
and end times. Scene lengths and the optional opening title duration supply exact
global offsets; silent scenes still advance the timeline. `SubtitleScene([], n)`
is for a deliberately silent scene, not a fallback for unavailable alignment.
Chinese text joins naturally without inserting spaces between wide characters.
`to_srt` emits bounded UTF-8 SRT with millisecond timestamps. HTML-like and ASS
control punctuation is converted to visible fullwidth punctuation, preventing
captions from turning into styling or positioning instructions when burned in.
The composer still owns secure paths, filter construction and media validation.

## Licensing, reliability and replacement

Most upstream files are **LGPLv3**; only its `srt_composer.py` is under MIT. This
project does not copy that composer and generates SRT through its own boundary
contract. Review the [upstream license](https://github.com/rany2/edge-tts/blob/7.3.1/LICENSE)
and applicable distribution obligations before shipping a packaged product.
The package license does not establish permission or service terms for Microsoft's
online service. No commercial SLA, availability guarantee, fixed voice inventory,
privacy commitment or long-term access is assumed. Keep live use disabled until
service suitability/terms are reviewed, and use a contracted replacement adapter
for commercialization where needed. A replacement must provide actual speech
alignment data, pass the same timing checks, and preserve secret isolation.

## Automated verification

`uv run pytest tests/test_tts.py` uses mocked streaming and child protocols only.
It covers English/Chinese boundaries, voice/rate validation, text/audio/metadata
limits, absolute deadline, missing/invalid alignment, process cancellation,
secret-free child configuration, safe errors, exact scene offsets, SRT bounds and
subtitle control neutralization. These tests do not prove live voice quality,
service access or permissions. Real audio decoding/output validation belongs to
the separate synthetic FFmpeg tests and live evaluation remains approval-gated.
