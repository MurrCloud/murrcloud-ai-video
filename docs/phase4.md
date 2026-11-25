# Phase 4 architecture and implementation checklist

The Phase 4 feature set extends the planning workflow. Live OpenAI/fal
acceptance remains pending; proceeding here does not claim those gates passed.
No shared host, production service, DNS, proxy, firewall or persistent infrastructure
change, paid generation or live speech request is authorized by these tests.

## Architecture

Use the existing project, immutable clip versions, assets and three worker queues.
An owner/editor reviews production settings and submits an idempotent render
snapshot containing exact clip IDs/checksums, narration, language/voice/rate,
transitions, export format, optional title cards, music and watermark. Preparation
runs in the narration queue, then dispatches to the dedicated rendering queue.
Paid OpenAI/fal calls are never part of a render retry.

Replaceable TTS adapters return audio plus speech boundaries. Edge TTS is the first
internal-evaluation adapter, separately disabled by default. English and Simplified
Chinese voices/rates are validated. Subtitles use speech boundaries, not character
count timing. Speech exceeding a scene duration requires an edit rather than
silently truncating narration. Retained narration assets may be reused by exact
content/settings fingerprint. SRT is downloadable and may be burned in.

FFmpeg is the sole compositor. Normalize scene resolution/FPS, transitions, audio,
narration, optional music, title cards, watermark and subtitles; export H.264/AAC
at 30 FPS, landscape 1920x1080, portrait 1080x1920 or square 1080x1080. Upscaling
requires explicit acknowledgement and remains visible in output metadata. FFprobe
and decoding verify integrity, duration, dimensions, FPS and required streams.
Treat all media/text as untrusted: generated local paths, no shell interpolation,
restricted protocols/demuxing, bounded subprocesses, scratch/output limits.

Worker concurrency and FFmpeg threads remain one. A shared storage lock prevents
parallel render processes across accidental duplicate workers. Host admission
fails closed before processing; resource deterioration pauses new work. Durable
leases/progress, bounded local retries and deterministic publication recover local
jobs without repeating paid video generation. Completed historical outputs remain
available; only a snapshot still matching the project may mark it completed.

Private local storage uses the existing quota/free-space lock. Optional S3 archive
support is configured only through backend secrets, with generated keys and private
objects; no bucket/service provisioning is included. Downloads and preview remain
owner-scoped. Regenerating one scene retains approved clips and previous outputs.

## Infrastructure constraints and proposed code changes

Use an independently assessed host and preserve dedicated container CPU/RAM/
process ceilings, private ports/networks/volumes and protected workloads.
Prepare FFmpeg/FFprobe and CJK fonts in the rendering image, a real narration queue
and separate render task time limits. Update isolated CI to exercise real synthetic
media renders, not protected workloads or paid providers. No deployment is performed.

## Checklist

- [x] Replaceable TTS contract, Edge adapter, speech-boundary subtitles and tests.
- [x] FFmpeg compositor, titles/music/watermark/transitions and FFprobe checks.
- [x] Explicit migration, render snapshot API, leases/progress/recovery/publication.
- [x] Private production asset upload/download, preview, optional S3 adapter.
- [x] Production settings/progress/video preview/SRT download UI.
- [x] Scene version selection/regeneration compatibility and historical outputs.
- [x] Local offline tests, real synthetic FFmpeg tests and isolated full-stack CI.
- [x] Operational documentation, verification checklists.

Missing live configuration: approved host signal/storage capacity, provider keys
and paid acceptance, an explicit decision to allow Edge network calls and its
licensing/reliability review, optional S3 endpoint/bucket/credentials, production
routing/TLS/DNS approval. Edge TTS has no assumed commercial SLA; adapter and voices
must remain replaceable. Full protected-workload acceptance requires separate approval.

Automated evidence: [phase4-validation.md](phase4-validation.md). Live acceptance
and production approval remain pending; see [phase4-operations.md](phase4-operations.md).
