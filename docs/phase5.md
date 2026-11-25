# Phase 5 architecture and implementation checklist

This feature set extends production with workspace and recovery controls.
Live provider and production acceptance are independent from synthetic tests.

## Plan

Reuse the FastAPI/PostgreSQL/Redis/three-queue architecture and Next.js UI. Add an
owner-scoped paginated library and dashboard, safe project duplication, private
or explicitly shared reusable templates, and cumulative usage/cost reports by
project, scene, model and provider. Copies/templates never carry paid approvals,
job IDs, private media references, credentials or committed spend into new work.
Archive preserves audit/history and blocks new production work, without deleting
media or silently cancelling charges. Changes use optimistic revisions, project
locks and durable mutation keys where appropriate.

Reports distinguish confirmed charge subtotals, missing/unknown charges, committed
reservations, project caps and remaining authorization. Unknown is not zero.
Operator charge confirmation/reconciliation must be explicit, role-protected,
audited and idempotent; it must not resubmit a paid request. Account settings may
change a password and revoke sessions, never expose/edit provider secrets.

Provide authenticated operational summaries for queue failures/leases/backlogs,
stale/missing host admission, storage pressure and unresolved charges. Host-wide
information is administrator-only. Use existing queues/signals and bounded queries;
no monitoring service, external alert message or new host agent is installed.
Prepare monitoring thresholds/runbooks and controlled release/rollback procedures.

Harden studio-only backup procedures and rehearse encrypted database/media backup
and restore against disposable infrastructure. Restore must refuse live or unrelated databases,
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
Do not store secrets in DB/templates, commit customer media or infer paid outcomes.

## Checklist

- [x] Library/dashboard pagination, filtering, archive and safe project duplication.
- [x] Reusable templates with ownership, explicit sharing and safe application.
- [x] Accurate usage/cost reports and explicit audited operator reconciliation.
- [x] Account/password settings and operational monitoring/status pages.
- [x] Encrypted studio backup/restore implementation and recovery documentation.
- [ ] Verify encrypted PostgreSQL/media restore in current isolated CI.
- [ ] Run current security, bounded load, failure and full isolated service/UI checks.
- [ ] Review operational handoff and acceptance gates for this staging commit.

Proposed infrastructure changes: application migration/API/UI, existing worker
instrumentation, operator scripts/examples and CI-only restore/load fixtures.
No new deployed service, resource allocation, port, volume, DNS or proxy route.

See [verification checklist](phase5-validation.md) and
[operator runbook](phase5-operations.md). Live paid quality, real speech/S3,
production restore and protected-workload capacity remain independent gates.
