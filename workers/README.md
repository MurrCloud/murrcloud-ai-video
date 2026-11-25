# Dedicated studio workers

`app.worker:celery_app` defines three isolated queues:

- `generation`: fal submissions/polls/cancellation, director calls, durable-job
  recovery. Paid flags and explicit project/operator approvals still apply.
- `narration`: `video.prepare_render`, exact narration caches and speech-boundary
  SRT; network speech is separately disabled by default.
- `rendering`: `video.render_video`, FFmpeg/FFprobe, private output publication and
  optional archive. One worker/FFmpeg thread plus a shared render lock.

Workers are opt-in Compose profiles with independent resource ceilings. Rendering
and narration images include FFmpeg and CJK fonts. The startup entrypoint and tasks
require fresh host admission; no missing/stale/bad signal fallback. Tracked flags
stay false, and no ERP network/mount/credential is shared. See
`docs/phase4-operations.md` for explicit local enablement, limits and recovery.

Phase 4 local retries never generate additional paid clips/director responses.
An interrupted unknown paid request still needs operator reconciliation. Local
render leases, publication intents and conditional archives recover separately.
