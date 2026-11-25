# Phase 3 design and execution boundaries

This feature adds local planning and an independently gated director. Live
provider acceptance is separate from mocked verification.

## Implementation

Add safe text/DOCX ingestion, owned reference images, an OpenAI Responses director,
versioned storyboard editing, scene validation and whole-plan cost approval.
Manual editing works without API keys. OpenAI structured output uses a strict
schema followed by independent capability, ownership, duration and length checks.
Scripts and generated text are untrusted data, never executable instructions.

The director has its own disabled-by-default gate, explicit configured model,
private OpenAI key, operator approval reference and per-request spending approval.
No live AI request is authorized for this increment. Automated tests use transport
mocks and block unmocked provider HTTP. Paid intent is persisted before an OpenAI
POST; uncertain results require reconciliation and never automatic paid retries.

Editor saves use optimistic revisions under a project lock. Immutable history and
scene versions retain earlier work. Scene add/remove/reorder and per-scene models,
durations, settings, prompts, narration, transitions and references are validated.
Archived scenes preserve their old jobs/media. Stale director results and estimates
cannot overwrite later edits or authorize a changed plan. Whole-plan approval must
reserve spending atomically and publish jobs only after the transaction commits.

## Ingestion boundary

DOCX: one bounded multipart upload, ZIP central-directory/member/decompressed-size
limits, no extraction, no macros/embedded programs/encryption or traversal, guarded
XML parsing with DTD/entities forbidden, and at most 100,000 extracted characters.
Only extracted text, a safe source name, checksum and warning codes are retained.
External document links are never fetched. The original archive is discarded.

References: one 5 MiB PNG/JPEG/WebP source, static only, at least 300px each side,
at most 8,388,608 pixels. Decode/canonicalization runs in a bounded subprocess,
strips metadata and preserves orientation without upscaling. Private UUID paths,
the shared storage quota lock, per-file limits and free-space reserve apply.
Only project-authorized preview routes expose canonical bytes.

References are planning attachments in this phase. Director requests include owned
IDs, captions and image metadata; image pixels and external URLs are not sent.
fal image-to-video stays disabled until secure provider reference delivery is
configured and verified. No S3, cloud-file publication or vision capability is
implied by local upload support.

## Infrastructure

No new service, database, network, public port, resource allocation, production
secret, proxy route or DNS change. Existing bounded API/worker containers handle
the new functions. JSON planning bodies are at most 1 MiB, multipart bodies at most
5 MiB plus framing, with two large-body slots and one image-processing slot. Login
password hashing uses one slot to conserve the existing API memory ceiling.

## Verification and remaining gates

Test malformed/bomb/macro/entity DOCX, non-image/animated/oversized images, access
control, stale revision and snapshot races, duration/model settings, immutable
history, atomic budget approval, interrupted AI requests and output validation.
Run Pytest/Ruff, frontend tests/types/production build and disposable PostgreSQL/
Redis/Celery/API/proxy checks in CI. No mock plan is presented as live AI output.
Live OpenAI and fal checks, production rollout and protected-workload tests remain
approval-gated. Record measured results in phase3-validation.md after completion.

Official API reference reviewed 2026-11-06:
https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses
