# Phase 4 operation and remaining deployment gates

Nothing in this increment is deployed. It prepares code, migration 0004 and
opt-in workers while tracked development/production defaults keep rendering,
narration, director, paid video and S3 disabled. The shared host remains untouched.

## Local or isolated development

Follow the README with dedicated studio credentials and explicit migrations. Use
`uv sync --frozen` and the Docker test/media images for exact dependencies. Local FFmpeg capabilities vary; Linux image verification is required for title
cards, burned subtitles and CJK fonts. The
render/narration images install FFmpeg, FFprobe, DejaVu and Noto CJK fonts.

Once reviewed clip generations are ready, open a project's **Produce video** page.
Choose exact clip versions (or the latest matching approved visuals), language,
voice/rate, narration/subtitles, 16:9 / 9:16 / 1:1, titles, music and watermark.
Save settings and review the output request. Saved settings/scene edits and
regeneration advance the revision; stale tabs cannot overwrite them. Request a
render with an idempotency key and complete scene-version selection. Retry an
uncertain HTTP response with the same key, rather than issuing another job. After
a confirmed current render failure, **New render attempt** deliberately uses a
fresh key after settings/capability review; it never submits paid video jobs.

A reviewed local Compose override may enable `VIDEO_RENDER_ENABLED=true` on API,
generation recovery, narration and rendering workers. This is application
configuration only and is not authority to change production infrastructure.
The private Docker network intentionally has no general outbound access. Live
provider/Edge/S3 use also needs a separately reviewed studio-only egress path; do
not join an protected workloads network as a shortcut.

Workers still require a fresh healthy host-admission signal; missing, stale or
bad evidence refuses new work. The existing worker entrypoint checks it before
startup, and tasks recheck it during work. Never use synthetic CI signals on the
VPS. Docker limits are ceilings, not reservations protecting protected-workload capacity.

Enable `VIDEO_NARRATION_ENABLED=true` separately only after authorizing Edge
network use for the intended text. No key is needed for Edge, but no commercial
service permission, SLA, availability or voice quality is inferred. See
`narration.md`. Use English or Simplified Chinese, a compatible listed voice and
0.5–2.0 speaking rate. Narration that exceeds a scene duration fails clearly;
edit the script/rate/duration. SRT uses speech boundaries, including exact scene
and opening-title offsets. With narration off, burned subtitles are off; timestamps
are never fabricated from text character counts. Silent scenes remain valid.

The preparation queue is `narration`; FFmpeg runs only in `rendering`, concurrency
one, one FFmpeg/filter thread. A shared file lock also serializes accidental extra
workers. Generation/director paid queues are independent and remain disabled.
Opening/closing cards add their configured duration to the scene total. Dissolve
and fade preserve scene timing. 1080p/30 FPS exports exceeding a source resolution
require explicit upscale acknowledgement; warnings remain in render metadata.
Uploaded music is MP3/WAV, at most 5 MiB, signature checked and decoded only by the
bounded worker. Watermarks use private canonical image uploads. User text is never
interpolated into shell commands, and media protocols are restricted to local files.

## Recovery, retention and resource limits

Render jobs contain immutable inputs, exact checksums, actor, stage, progress,
leases and output reports. Local retry is bounded with backoff; neither narration
nor render retries submit paid video/director generations. A lost worker lease
is recovered independently of the enable gate. Recently queued jobs do not
starve interrupted jobs. Disabling/demoting the requester or cancelling a job
prevents further authorized publication/completion.

The worker records pending asset metadata before publishing. Published narration
can rebuild its alignment cache after an interrupted commit; a published final
MP4 is reused for archive retry. Unknown/corrupt ready media fails closed. Source
clips and old scene versions are retained; old render outputs remain privately
available, but only a snapshot matching the current project marks it completed.

Source/assets live in the separate studio volume under generated UUID paths.
Publication shares the existing 2 GiB quota and 512 MiB free-space reserve. Scratch
is at most 128 MiB and lives in the rendering tmpfs, with exact job directories
cleaned on retries. Long/high-bitrate projects may exceed this conservative ceiling
and fail rather than exhaust the host. Do not raise it on the VPS without capacity
review. Never mount unrelated paths or the Docker socket. Do not recursively delete
pending or historical assets as an improvised cleanup policy; resolve their DB
records and backup/retention requirements first.

FFprobe checks video/audio, dimensions, FPS and duration; a full decode verifies
integrity. Normal timeouts/cancellation terminate child process groups, and Linux
parent-death signals kill FFmpeg/Edge/S3 children if their worker disappears.
FFmpeg tasks have separate 3660/3720-second soft/hard limits; broker visibility
is 7200 seconds. Preparation has 1200/1260-second limits and exact narration caches.

## Optional private object archive

S3 is disabled unless explicitly configured with `VIDEO_S3_ENABLED`, HTTPS
`VIDEO_S3_ENDPOINT_URL`, `VIDEO_S3_BUCKET`, region and dedicated backend access/
secret keys. No bucket is created here. Confirm a private bucket/policy and scoped
credentials, costs, lifecycle and restore before enabling it. The adapter uses
conditional object creation and checksum metadata; repeat writes reconcile the
existing object's length/checksum rather than silently overwrite it. The S3 SDK
runs in a bounded child with a 90-second absolute deadline and no ambient API keys
or proxy credentials. No DB transaction is held during upload.

Finished MP4/SRT assets are archived when enabled. Quota-bound local hot copies
remain the authenticated preview/download source; archive-only download/restore
and hot-cache eviction are not implemented. Archive failure pauses or fails the
job with the validated local output retained for retry. Missing archive prices
are not recorded as zero provider charges. No live bucket compatibility, costs,
restore or credentials were verified in this phase.

## Production acceptance still required

Rehearse migration 0004 on a retained-data copy and verify backups/restores. Review
real peak-hour host capacity, quotas/retention, the real host signal, protected-workload latency
and the existing proxy/TLS/DNS candidate. Live cloud video/AI director quality,
real Edge narration and complete paid-script acceptance require separate approval
and credentials; synthetic tests cannot establish them. Test F needs an expressly
approved before/during/after protected-workload trial. Phase 5 adds library/templates/cost reporting/monitoring and recovery controls. No production
service, DNS, proxy, firewall, database or persistent volume change is authorized
by passing this phase's tests.

Official archive contracts reviewed 2026-11-06:
[Boto3 PutObject](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html)
and [conditional S3 writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html).
These references validate request semantics, not a particular S3-compatible provider.
