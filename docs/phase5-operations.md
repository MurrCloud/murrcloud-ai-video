# Phase 5 operator runbook

Use a reviewed source commit and scoped authorization before production changes.
Complete the private assessment and deployment checklist separately.

## Access and routine use

Administrators bootstrap users with the existing password-prompt CLI; there is no
public registration. Editors manage their projects and private templates. Viewers
can read their own workspace and visible shared templates. Administrators have
global operational access. Project IDs, asset URLs, report filters and mutation
requests all retain backend authorization. Password changes require the current
password, an allowed Origin and rate limiting; all account sessions are revoked.
Administrators may stage encrypted provider keys in Settings; runtime credentials
remain independently configured on the backend. See provider-key-settings.md.

Library searches are case-insensitive literal searches with bounded pagination.
Archive refuses unresolved paid work or an active render, keeps media/history and
blocks new generation, ingestion and rendering. Restoring a library item simply
reopens it. Duplicate/template application creates a fresh draft, without approved
caps, committed spend, jobs, lineage IDs or private references/music/logo. Review
the new plan, attach media and explicitly approve its own cost estimate. Shared
template text may contain proprietary script/prompt content: only an administrator
can publish it after explicit content acknowledgement. Apply, edit and save a new
template to change its production content; metadata changes use revisions.

## Cost interpretation and verified recovery

Usage reports are cumulative, including archived projects. Group by project,
scene, model or provider; filters and totals use the same owner scope. Confirmed
USD is a verified subtotal. Missing, unavailable and pending charges stay visible;
the total actual cost is unavailable until every charge is known. A verified zero
is distinct from no bill. The catalog estimate is not a provider invoice.

Committed USD is the held reservation ledger. Spending exposure adds each job's
verified charge overrun beyond its reservation and any unreserved confirmed
charge. Remaining configured budget and remaining approved authorization subtract
that exposure, and may be negative. A lower bill or a verified failure never
releases a reservation automatically. An overrun never raises approval or budget;
new work is blocked when exposure plus its proposed reservation would exceed the
configured budget or approved cap. The user must review and grant sufficient
authorization before proceeding. Reporting a bill is evidence of spending, not
permission to spend.

Operations is administrator-only. Resolve ambiguous jobs with actual provider
dashboard/support evidence. Never use a new submission to discover whether a
prior paid request succeeded. Each reviewed action binds an expected record
timestamp, durable mutation key and evidence reference, with an audit event.

1. Inspect generation and director using the uncertain-job-type selector, including project,
   model, reservation and existing provider ID. Verify the provider account,
   endpoint, request ownership and final outcome outside this automated workflow.
2. For a known fal request, attach its canonical UUID. The adapter constructs only
   verified queue status/result/cancel URLs and dispatches polling of that existing
   request. A previous local cancellation request is honored. This action never
   authorizes a new submission, nor proves a request is free or cancellable.
3. If failure is verified, mark it failed with a support/statement reference. The
   original ambiguous attempt and reservation remain. A director's lost plan
   cannot be reconstructed from its response ID here; resolve the old outcome
   first, then explicitly approve any new attempt as separate paid work.
4. After the job is terminal, confirm its actual charge against a provider bill.
   Enter the amount explicitly (including zero), an optional unique charge ID,
   reference and verification acknowledgement. Confirmed bills are immutable
   through this API. Correct billing disputes through reviewed data procedures,
   preserving evidence; never edit SQL ad hoc to free budget.
5. Refresh Usage, Operations and audit history. On a revision/key conflict, reload
   and review the current record. Repeating exactly the same durable key is safe.

No provider billing API is assumed to exist; confirmation is manual evidence.
No action in this workflow calls a paid submission endpoint. Real provider reads
still need valid credentials, reviewed egress and authorized operational use.

## Monitoring and incident response

Operations returns a fresh database summary. Queue counts describe persisted job
states, not Redis depth. Failed counts cover the previous 24 hours. Worker presence
uses existing Celery worker-ready/heartbeat signals, a separate one-connection
instrumentation pool and a maximum write rate of once per 30 seconds per process.
A 90-second freshness window is used; instrumentation rows older than seven days
are pruned during successful heartbeat writes, without a separate cleanup timer.
Missing enabled workers, ambiguous requests, unconfirmed charges, expired render
leases, quota pressure and blocked admission produce in-app alerts. No external
message, monitoring service or host agent is installed. Refresh the snapshot for
current evidence; there is no unattended on-call delivery guarantee.

Storage evidence describes the Studio volume's apparent file bytes and filesystem
free space. A bounded scan (20,000 entries / 250 ms) refuses unsafe/incomplete
evidence; it does not claim a filesystem quota is installed. Alert at 90% of the
configured Studio storage quota or below the configured free-space floor. Existing
publication/scratch/process caps and locks remain enforced independently.

Host admission remains fail-closed: signal age <=90 seconds, available host memory
>=2 GiB, root free space >=15 GiB, load <=0.75 per core, protected services healthy,
protected-workload HTTP 200 <=1 second. These conservative defaults do not prove capacity. Missing
or stale signal blocks new renders. Do not disable admission to clear an alert.

Keep the configured latency/load/memory/disk admission thresholds until reviewed.
For protected-workload errors, timeouts, unexpected restarts or critical resource
pressure, pause new Studio work and use its scoped stop procedure. Preserve
volumes, request identities and unknown charges. Never restart unrelated services.
Diagnose Studio dependency failure from readiness/worker evidence and
safe codes; do not dump resolved secrets, provider bodies or customer scripts.
Reconcile lost paid intent before resuming. Local render retries retain leases,
immutable snapshots and a single rendering worker; paid calls are never retried
automatically after an ambiguous outcome.

## Backup, release and rollback gates

Follow [encrypted backup and isolated recovery](backup-recovery.md). Production
quiescence, separate backup filesystem/quota, off-host encrypted custody, retention,
RPO/RTO, recovery keys and restore promotion need a named operator and approval.
The CLI has no service-management or production-promotion command. Real CI restore
uses only newly created disposable Studio databases and synthetic media.

Before release, review migrations through 0006 on retained test data, exact locked images,
all automated evidence and current host assessment. This migration adds library
archive/revision state, durable workspace mutation keys and worker presence. No
new production service, volume, network, port or resource allocation is proposed.
Existing container ceilings do not reserve CPU/RAM for protected workloads. Keep one render and
one FFmpeg/filter thread, private DB/Redis, non-root users, dedicated credentials,
separate storage and the private network. Explicitly review future outbound egress;
the tracked internal network does not provide live provider connectivity.

Live English/Chinese video quality, two paid models, multilingual speech quality,
S3 compatibility/completeness, actual protected-workload impact, production quotas and
backup restoration are still external acceptance gates. HTTPS/certificates,
the chosen example hostname and DNS and the existing reverse proxy require a separate exact
plan and approval. Prepare migration/rollout/rollback steps and observe protected workloads before,
during and after the approved trial. Do not use the live shared host as a CI runner.
The trial must verify protected-workload functionality, availability and data integrity; record
latency changes against explicit acceptance thresholds.
Resource exhaustion can still interrupt a shared host despite application
separation, so this trial and the capacity/rollback plan remain required.

On a failed rollout, stop only approved Studio services, preserve volumes/logs,
and use a schema-compatible application image rollback. Never run `down --volumes`
or automatic production Alembic downgrade. An isolated fenced restore is a separate
reviewed recovery path, not an automatic traffic cutover.

## Integration references

- [fal queue status](https://fal.ai/docs/api-reference/inference-api/queue/status),
  [result](https://fal.ai/docs/api-reference/inference-api/queue/result) and
  [cancellation](https://fal.ai/docs/api-reference/inference-api/queue/cancel):
  owner/app request paths omit the endpoint subpath.
- [Celery signals](https://docs.celeryq.dev/en/stable/userguide/signals.html):
  worker-ready and heartbeat-sent presence hooks.
