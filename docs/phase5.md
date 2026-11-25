# Phase 5 implementation and acceptance status

This feature set implements workspace and recovery controls. Isolated service
acceptance has exercised the implementation; live provider and deployment
acceptance remain separate gates. Source release does not enable remote features
or establish production readiness.

## Implemented behavior

The FastAPI/PostgreSQL/Redis/three-queue architecture and Next.js UI provide an
owner-scoped paginated library and dashboard, safe project duplication, private
or explicitly shared reusable templates, and cumulative usage/cost reports by
project, scene, model and provider. Copies/templates never carry paid approvals,
job IDs, private media references, credentials or committed spend into new work.
Archive preserves audit/history and blocks new production work, without deleting
media or silently cancelling charges. Changes use optimistic revisions, project
locks and durable mutation keys where appropriate.

Reports distinguish confirmed charge subtotals, missing/unknown charges, committed
reservations, project caps and remaining authorization. Unknown is not zero.
Operator charge confirmation/reconciliation is explicit, role-protected,
audited and idempotent; it must not resubmit a paid request. Account settings may
change a password and revoke sessions. Separately, administrator-only
[provider-key Settings](provider-key-settings.md) stage encrypted, inactive keys;
they never reveal saved values or enable spending.

Authenticated operational summaries cover queue failures/leases/backlogs,
stale/missing host admission, storage pressure and unresolved charges. Host-wide
information is administrator-only. Use existing queues/signals and bounded queries;
no monitoring service, external alert message or new host agent is installed.
Monitoring thresholds/runbooks and controlled release/rollback procedures are
documented; no unattended alert delivery or host installation is implied.

Studio-only backup procedures support encrypted database/media backup and restore,
with a real disposable PostgreSQL/age roundtrip in isolated acceptance. Restore
refuses live or unrelated databases,
existing targets, unsafe archives and untrusted hashes. Backup scripts never stop
or change services automatically. Consistent production capture, off-host custody,
retention and restore rehearsals still require approved operator arrangements.

## Constraints, risks and missing configuration

Complete a current private host assessment before any deployment.
Preserve the existing independent containers/networks/volumes/secrets, resource
ceilings, one render/FFmpeg thread and fail-closed admission. No unrelated files or Docker
socket enter containers. Test and build only locally or in disposable GitHub CI.

Keys/live model and speech acceptance, private S3 compatibility/restore, approved
egress, host capacity/quota/retention, backup recipient/key custody, actual protected workloads
load acceptance, production HTTPS/routing and rollout approval remain missing.
Never store plaintext provider keys in the database or any keys in templates.
Keep customer media outside Git and never infer paid outcomes. The administrator
key vault stores only authenticated ciphertext under an independent API-only
encryption master.

## Implementation checklist

- [x] Library/dashboard pagination, filtering, archive and safe project duplication.
- [x] Reusable templates with ownership, explicit sharing and safe application.
- [x] Accurate usage/cost reports and explicit audited operator reconciliation.
- [x] Account/password settings and operational monitoring/status pages.
- [x] Encrypted studio backup/restore implementation and recovery documentation.

## Isolated acceptance and per-commit evidence

Isolated acceptance has verified real PostgreSQL migrations and concurrency,
Redis/Celery transport, encrypted PostgreSQL/media restore with recovery fencing,
bounded reads and failure/security cases, synthetic FFmpeg work, frontend tests,
typecheck and production build, live disposable API/proxy behavior, container
isolation, restart and dependency outage handling. These gates have existing
isolated acceptance evidence.

Verification applies to the exact source commit tested. Before a source release,
require a successful **Studio isolated acceptance** workflow for the reviewed
commit and inspect its failures, warnings and skips. Frontend-surface cases skipped
before frontend startup must pass in the later live-service checks. A previous
successful run cannot establish acceptance for subsequent changes.

The [verification checklist](phase5-validation.md) is a reusable reproduction and
result-record template. Its blank result cells are not a statement that no CI has
run. Keep current per-commit evidence in the corresponding Actions run and review
record; do not fill reusable templates with private deployment provenance.

## Independent live and deployment gates

Live model/director and speech quality, actual provider charges, optional S3
compatibility and complete recovery, retained-data migration, real host capacity,
production backup custody/restore and rollout remain unaccepted by isolated CI.
The S3 adapter archives uploads only: archive-only download/restore and local-cache
eviction are not implemented. The backup CLI supports local media and rejects
non-local media backends. See [archive limits](phase4-operations.md) and
[backup scope](backup-recovery.md) before selecting a deployment storage model.

Each live or infrastructure gate needs its own reviewed configuration, evidence
and authorization. Keep paid generation, director, narration, rendering and S3
disabled, runtime provider keys blank and workers opt-in until their applicable
gates are satisfied. A source-release decision grants no permission to run these
workloads or change production.

Proposed infrastructure changes: application migration/API/UI, existing worker
instrumentation, operator scripts/examples and CI-only restore/load fixtures.
No new deployed service, resource allocation, port, volume, DNS or proxy route.

See the [operator runbook](phase5-operations.md) for the release, recovery,
capacity and rollback requirements.
