# Phase 2: selectable cloud clips

Implementation scope: verified catalog, fal adapter, Next.js model selector,
approval-bound generation jobs, polling, local clip downloads and recovery.
No production deployment or paid generation is authorized or performed.
Later phases implement the director/narration/FFmpeg pipeline.

## Model catalog and frontend

The explicit `python -m app.cli seed-models` command inserts verified Wan2.7 and
Kling3 Pro text/image endpoints into PostgreSQL, preserving operator edits to
pricing, defaults, enablement and provider settings. Text models are enabled;
image models show their capabilities but remain disabled until owned reference
ingestion is implemented. See model-research.md and the reviewed schema extracts
in app/providers/schemas. Catalog prices include provenance and verification time.

Next.js/TypeScript/Tailwind provides login, dashboard, project creation, dynamic
model selection, cost review, job progress, cancellation and private clip links.
Wan native audio is always on; Kling has a toggle. Wan sends an integer duration
and resolution; Kling sends a string duration and omits unsupported resolution
parameters. Capability changes reset incompatible UI settings. Viewers cannot
create or approve jobs; administrators can switch supported model defaults and
enablement through the authenticated API. Provider keys never enter the frontend.

## Approval and spending

1. Estimate a specific model/request, optional existing scene, retry allowance
   and current project settings. Persist the hash and an expiring snapshot.
2. Approve the estimate with a unique idempotency key and total PROJECT spending
   cap (bounded by the project budget). The cap is not a per-job quote.
3. Under a project-row lock, reject stale/changed/consumed estimates, disabled
   models/providers, exhausted budgets and unresolved prior jobs for the scene.
   Create a new immutable scene version and durable job, reserve cost, and commit
   before publishing to Celery. A broker outage leaves retryable dispatch state.
4. The worker requires operator and project approval plus fresh host admission.
   It commits submission intent and an attempt before a single paid POST.

Public model-page quotes are estimates. Authenticated pricing refresh retrieves
the account BASE unit; it does not prove exact resolution/audio variant pricing.
Unverified and unavailable pricing require explicit acknowledgement and a positive
cap; the entire remaining approved cap is reserved conservatively. A retry
allowance affects estimates, not authorization to repeat a paid request.

Actual charges are null/unavailable until reconciled with billing; zero is never
substituted. Cancellations, failures and uncertain outcomes retain reservations.
There is no automatic refund or assumption that cancellation stopped billing.
Choose a deliberate initial project cap: later approved caps may grow only within
the original project budget. General budget editing/billing reconciliation UI is
later-phase work. Local caps control commitments; unavailable provider pricing
does not establish a provider-enforced maximum charge.

## Provider and job recovery

The provider abstraction owns validation, estimates, submission, status, results
and cancellation. Queue coordinates returned by fal are validated against the
canonical owner/app/request path and persisted. Requests use explicit timeouts,
an absolute 45-second deadline and X-Fal-No-Retry:1; no automatic paid POST retry.

Unknown submission outcomes enter reconciliation_required and block regeneration
of that scene. They require manual account/provider investigation; creating a new
job is not reconciliation. Known provider IDs are polled using bounded exponential
backoff. Interrupted poll/download leases are reclaimed after expiry; recovery
dispatches only the existing ID's safe polling path. Project-first row locks
serialize approvals, completion and scene regeneration. Old versions and costs
remain available. Local cancellation during a download prevents publication as a
wanted output. Completed scene clips set project state clips_ready, never final
video completed (reserved for a later assembled/exported result).

The generation worker runs one prefork child and a bounded recovery scheduler.
Rendering remains disabled. The shared host admission policy is checked at worker
startup, before paid submission and before media download. Default Compose gates
remain false and its network remains internal; no cloud egress was enabled.

## Storage boundary

Only provider-returned HTTPS fal.media URLs are accepted. DNS runs in a bounded
resolver subprocess with no provider secrets; all addresses must be public. The
HTTP connection pins the checked IP and retains TLS hostname/SNI verification.
It sends no API authorization to media hosts and never follows redirects.

Downloads enforce content type, MP4 signature, per-file bytes, application storage
quota, free-space reserve and an absolute socket deadline across TLS/headers/body.
Atomic server-generated UUID paths and a quota lock make completion idempotent.
Authenticated asset routes check project ownership and never expose arbitrary
filesystem paths. These clip checks do not replace final FFprobe stream/duration
validation in Phase4. Local storage is implemented; S3 lifecycle is still pending.

## Isolated local setup

1. Configure private dedicated secrets per README; keep both paid/render flags false.
2. `uv sync --frozen`; build the API and migrate explicitly to head0002.
3. `docker compose run --rm api python -m app.cli seed-models`
4. Bootstrap an admin with the existing password-prompt CLI.
5. `docker compose --profile ui up -d --build api frontend`
6. Open http://127.0.0.1:3100. Provider validation and public estimates work without
   a fal key. Generation approval is blocked while the operator gate is false.

For frontend-only development, run npm ci, npm test, npm run typecheck and npm
run build in apps/frontend, then npm run dev. API_BASE_URL is server-only and
baked into the Next.js rewrite at build time; Docker uses http://api:8100.

After a separate explicit paid-test approval, an operator would need dedicated
VIDEO_FAL_KEY and VIDEO_PAID_APPROVAL_REFERENCE secrets, an approved egress/network
override, a fresh host signal, approved storage/quotas and a controlled spending
cap before enabling generation. This repository does not enable those changes.

## Verification gate

Local Python tests and mocked provider contracts run without real cloud requests.
The automated suite blocks unmocked async HTTP transports. The Docker CI workflow
uses ephemeral credentials and a private network, verifies real PostgreSQL
concurrency/migrations, Redis/Celery health-task transport, Next.js build/tests,
same-origin login/catalog/estimate proxying and the disabled paid gate.

Live acceptance A/B (real selected-model clips and download) is explicitly pending
approval and credentials. No mocked fixture is presented as a generated video.
Narration, director, render, S3 and protected-workload acceptance are independent
gates. The verification checklist is in phase2-validation.md.
