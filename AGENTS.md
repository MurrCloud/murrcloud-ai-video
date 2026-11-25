# Development instructions

Read docs/architecture.md and docs/server-assessment.md before changes.

- Python 3.12+, FastAPI, SQLAlchemy 2, psycopg 3, Alembic, PostgreSQL, Redis/Celery.
- Frontend: Next.js, TypeScript, Tailwind; no Remotion. Brand primary #FF7D00.
- Keep provider request translation in adapters; do not hardcode one model.
- No invented fal endpoints/prices. Verify current official docs and retain source
  URL + verification timestamp before enabling catalog entries.
- No paid API submissions without explicit approval. Unknown prices are never zero.
- Paid submission retries require provider reconciliation; persist intent, IDs,
  reservations, attempts and scene versions. Test interruption recovery first.
- Preserve other workloads, databases, routes, credentials, backups and storage.
  Do not restart shared services for Studio deployment. Production and persistent
  infrastructure changes require explicit approval within their scope.
- Production or persistent infrastructure changes require separate, scoped
  operator authorization. Keep paid video, director, narration, rendering and
  S3 disabled by default, provider secrets blank and workers opt-in.
- Preserve other workloads, routes, credentials, backups and storage. Never use
  a live host to substitute for missing isolated test tooling.
- Do not run Docker builds or automated suites on a shared host to work around missing local
  tooling. Import verified images built and tested in isolated CI instead.
- Do not share other workloads secrets, services, mounts, network, database, or Docker socket.
- Default rendering concurrency/FFmpeg threads: one. Admission fails closed if host
  health is missing/stale/bad. Container ceilings do not reserve capacity for other workloads.
- Do not commit secrets, customer media, generated video, runtime state or `.env`.
- Provider keys saved in Settings are staged ciphertext in `provider_secrets`.
  Keep their independent encryption master API-only and outside DB/media backups.
  Never expose stored key values/fragments, call providers when saving, promote
  them automatically, or add paid-enable/test controls without explicit approval.
  Preserve actor/session locks, password reauthentication, revision fencing and
  secret-free errors/audits. Restore clears ciphertext and advances tombstones.
- Use explicit Alembic migrations; do not create schema at API startup. Keep checks
  and migration snapshots consistent; test PostgreSQL drift and rollback.
- Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`.
  Run isolated PostgreSQL/Redis/Compose integration before claiming Phase 1 passed.
- Do not claim paid generation, render, or other workloads load tests passed using mocks.
- Work incrementally; document each acceptance gate, deferred feature and blocker.
- Phase 3 director calls are separately gated. Keep the OpenAI key backend-only,
  configure a model explicitly, and retain unknown charges and paid intent.
  Ambiguous calls must never be repeated automatically.
- Treat DOCX, image metadata and AI output as untrusted. Preserve upload/body,
  XML/ZIP, decoder subprocess, storage quota and concurrency limits.
- References are private planning attachments. Do not enable fal image-to-video
  or send image pixels until secure delivery and its acceptance checks are added.
- Storyboard saves and approvals must lock the project, check the expected
  revision, preserve history and commit all scene reservations before dispatch.
- Phase 4 media work belongs to narration/rendering queues, never HTTP handlers.
  Keep one render and one FFmpeg/filter thread; preserve host admission, leases,
  global media locks, process groups/parent-death guards and scratch/output caps.
- Render immutable clip/version/checksum snapshots. Recheck actor and cancellation
  before external calls/publication, and only mark the current project completed.
  Recover published pending media before repeating narration or recomposing.
- Speech timing must come from boundaries/alignment. Never invent character-count
  timestamps; reject narration that exceeds scene duration. Edge has no assumed
  commercial SLA. Real Edge/S3 tests require separate authorization.
- Keep S3 private/conditional and backend-only, with an absolute child deadline.
  Local hot copies stay quota-bound; do not claim archive restore/eviction exists.
- Library/template copies start as fresh drafts with no approvals, costs, provider
  IDs or private media references. Sharing requires explicit administrator review.
  Preserve owner scope, pagination, revision checks and actor/project lock order.
- Billing confirmation is administrator-only, evidence-bound, audited and durable.
  Never infer zero, release reservations, increase approved caps or resubmit paid
  intent during reconciliation. Include verified overruns in spending exposure.
- Late director responses must own the submitting job/attempt before changing its
  plan, status or charge; preserve an operator's resolution and confirmed bill.
- Backups are encrypted and Studio-only. Restore only to a new isolated empty
  recovery database/media path with a trusted ciphertext hash; revoke sessions and
  fence restored jobs. Never promote a restore, install host tools, arrange live
  quiescence or schedule backups without explicit infrastructure approval.
