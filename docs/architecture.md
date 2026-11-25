# Architecture and acceptance gates

This source copy contains the Phase 5 feature set. Its own staging test results
must be verified independently; historical private acceptance evidence is omitted.
See phase2.md through phase5.md for feature designs.

## System

Next.js / TypeScript / Tailwind provide the internal studio. A same-origin
FastAPI service owns authentication and authorization. PostgreSQL stores users,
projects/settings, scenes and immutable versions, configurable models/providers,
generation jobs/attempts, assets, render jobs, estimates/usage, templates, and audit
events. Active provider secrets are environment references, never plaintext DB
fields. Administrator Settings can stage provider-bound authenticated ciphertext
in a separate table; its independent encryption master is supplied only to the
API. Staging does not activate credentials or grant spending approval. See
[provider key settings](provider-key-settings.md).
Redis is a dedicated broker and distributed rate limiter. Separate Celery queues
serve cloud generation, narration, and FFmpeg rendering. S3-compatible storage
holds durable artifacts; bounded local scratch space holds intermediate media.

Provider interfaces isolate endpoint-specific validation, settings, pricing,
submission, reconciliation, and cancellation. The model catalog is deliberately
empty in Phase 1: verified fal.ai endpoints and pricing are a Phase 2 gate.
No fake video generation, guessed prices, or paid API calls are part of Phase 1.

## Reliability and spending design

Before paid work, lock the project row, validate the approved estimate/version,
reserve spending, and create a uniquely keyed job. Persist submission intent
before contacting a provider. An interrupted submission with unknown outcome
must enter reconciliation_required; never blindly retry a paid POST. Store
provider job IDs uniquely and poll/reconcile existing jobs. Scene regeneration
creates a version and retains earlier outputs. Budget estimates, committed
amounts, actual charges, unknown prices, and local cancellation remain distinct.
These are design constraints; production orchestration is implemented in later
phases and cannot be inferred from the presence of database tables.

## Trust boundaries

Use Argon2 passwords, expiring opaque server-side sessions, HttpOnly cookies,
explicit origins, role checks, and owner-scoped project queries. Redis limits
login/API traffic and failures close access rather than silently disabling limits.
Bootstrap the first admin through a local CLI, with no public registration or
default password. Health checks reveal only availability, not credentials.

Uploaded files, DOCX archives, external URLs, generated prompts, and FFmpeg
arguments are untrusted. Upload/SSRF/media processing controls must pass their
tests before these features become reachable. No upload or arbitrary URL fetch
endpoint is exposed in Phase 1. API keys never enter frontend bundles.

## Shared-host isolation

Use a separate Compose project, PostgreSQL, Redis, independent secrets, network
and volumes. Do not mount unrelated application files, credentials or the Docker
socket. Database/cache ports remain private. Migrations run explicitly against
the dedicated Studio database; startup never creates tables automatically.

Container resource limits are ceilings, not capacity reservations. Keep workers
opt-in with one render and one FFmpeg/filter thread. Host memory/disk/load and
protected-workload health must be fresh and healthy; missing, stale or bad signals
deny admission. Use a separately reviewed filesystem/quota, bounded scratch,
retention, off-host encrypted backup and tested isolated restore. Example limits
are configuration templates, not measurements or approval for any host.

The initial deployment template starts only PostgreSQL, Redis, API and frontend.
Paid video, director, narration, rendering and S3 remain disabled, runtime provider
keys blank and workers stopped. Each later enablement needs its own reviewed
configuration and acceptance. See [deployment checklist](deployment-checklist.md).

## Implemented features and independent gates

- Foundation: authentication/ownership, explicit migrations, dedicated containers,
  fail-closed Redis limiting and host admission.
- Cloud clips: catalog/adapters, model settings, estimates, durable spending
  reservations, immutable versions and interrupted-intent recovery.
- Planning: bounded DOCX/images, structured director, versioned storyboard,
  scene validation and atomic whole-plan approval.
- Production: replaceable TTS, real timing/SRT, serial FFmpeg, immutable snapshots,
  private previews, bounded uploads, recovery and optional private archive.
- Workspace: library/templates/reports, account settings, administrator monitoring,
  verified billing reconciliation and encrypted fenced recovery.
- Provider Settings: administrator/password/revision-protected encrypted staging,
  API-only independent master and restore ciphertext clearing/tombstone fencing.

Current backend/frontend, typecheck, production build and isolated PostgreSQL/
Redis/Compose checks must pass for the staging commit. Record failures and skips.
Live cloud/director/Edge quality, S3 compatibility, paid end-to-end output, actual
shared-host capacity and production recovery are separate external gates.
Synthetic fixtures cannot prove them. Never use a live host as an isolated runner.

## Configuration required before deployment

Generate independent database/cache/session/admin secrets privately. Configure
origins, trusted proxy peers, host identity/protected-service probes, dedicated
storage, image/source provenance and private backup custody. Keep all completed
host assessments and operational evidence outside this repository.

Later remote workflows additionally need reviewed provider keys/models/pricing,
spending authorization, private egress/storage, commercial voice rights and
reliability review. Edge has no assumed commercial SLA. Image-to-video remains
disabled pending secure reference delivery. Archive restore/eviction is not implied
by the optional private upload adapter.

## Official references

- FastAPI authentication: https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/
- SQLAlchemy schema: https://docs.sqlalchemy.org/en/20/orm/declarative_tables.html
- Alembic: https://alembic.sqlalchemy.org/en/latest/tutorial.html
- Celery acknowledgements: https://docs.celeryq.dev/en/stable/userguide/tasks.html
- Compose controls: https://docs.docker.com/reference/compose-file/services/
