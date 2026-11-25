# Controlled deployment prerequisites and operations

This is a preparation runbook. No command here has been executed on production.
Preserve protected workloads. Production/persistent infrastructure changes need
explicit approval for the exact plan, commit, resources and rollback.

## Before approval

1. Review docs/server-assessment.md and collect representative peak-hour host
   CPU/memory/disk/I/O plus protected-workload health latency. Reconfirm port8100 and capacities.
2. Verify an independent recoverable unrelated database/filestore/config backup and VPS
   recovery point without using Video Studio to modify them. Identify owner/RPO.
3. Prepare studio-only database/cache/session secrets and operator admin details.
   Never reuse unrelated service passwords. Protect the environment file outside source.
4. Review all image tags, current security updates and record resolved image
   digests for rollout. Hash-locked Python dependencies are in requirements.lock.
5. Approve dedicated persistent storage capacity, quota/disk-monitoring strategy,
   retention, encrypted off-host backup destination and recovery key custody.
   Confirm the target filesystem has an independently reviewed quota or hard bound.
6. Verify Compose runtime and real PostgreSQL/Redis/API tests on an isolated host.
   Do not use the shared host as a CI runner or perform rendering to test this gate.
7. Review host collector thresholds, paths and units. Production mounts only its
   nonsecret signal file read-only; a missing/stale signal blocks workers.
8. Prepare and review an exact change plan: separate Compose project, dedicated
   volumes/network, loopback API, explicit migration, admin bootstrap and health
   verification. Default worker profiles remain off; paid/render gates remain off.

## After explicit controlled-deployment approval

Use both Compose files and the fixed project name `murrcloud-video-studio`.
Stage the reviewed commit and private environment in a separate approved path;
do not mount or copy unrelated directories. Render `docker compose config --quiet`
without printing resolved secrets. Review resource/security flags, existing host
listeners, new volume ownership and image digests. Start only postgres/redis,
run the explicit Alembic migration against video_studio, bootstrap a studio admin
using the password prompt, then start API. Confirm `/health/ready` using loopback.
Do not enable workers or paid/render flags during the initial service rollout.
Later implementation is present; its live gates remain separately controlled.

Observe host available memory, filesystem capacity and protected-workload latency before/during/
after every startup. A failed studio health check does not justify restarting protected workloads,
native PostgreSQL, Docker daemon, unrelated authentication services, or Nginx. Stop only the new studio services
under the approved procedure if protected-workload functions fail, requests time out, services
restart unexpectedly or critical memory/disk pressure develops. Record slowdown against the operator's explicit capacity and acceptance thresholds. Keep existing
admission thresholds until separately reviewed. Treat ceilings as contention
limits, not reserved headroom. Persistent data growth is not bounded by container
memory. Shared-server resource exhaustion can still interrupt protected workloads, so verify
functional acceptance during the approved trial and retain Studio-only rollback.

## HTTPS routing (separate approval)

Preserve the selected reverse proxy, certificate manager and all unrelated routes.
`infrastructure/nginx/video-studio.private.conf.example` is a loopback-only
candidate. `video.example.test.conf.example` prepares an uninstalled HTTPS domain
candidate with separate loopback API/frontend upstreams; the frontend/certificates
need separately verified production certificates. Verify DNS, certificate
approach, same-origin API routing, explicit trusted proxy addresses, body limits
and timeouts. Compare full candidate routing, run `nginx -t`, and prepare rollback
before seeking approval to install/reload. No certificate issuance, production
DNS, proxy or firewall rule change is included in Phase 1.

## Backup and rollback

See scripts/restore.md. Backups require an owned private directory on a separate
filesystem, encrypted archives, bounded staging, retained free space and an
off-host destination. Test a restore into independent credentials/volumes first.
No backup schedule or off-host service is provisioned by this repository.

On rollout failure, stop only the studio API/workers; preserve studio volumes and
logs. Do not use `down --volumes` on a deployed project. Roll back application
images only when schema-compatible; do not run production Alembic downgrade as
an automatic recovery step. Plan DB restoration separately from a reviewed backup.

## Implementation and remaining live acceptance

Phase 2 provides the frontend, verified catalog/adapters, estimates, approval,
reservations, cloud job recovery and local clip downloads behind disabled paid
gates. Live model acceptance still needs explicit approval and credentials.
Phases 3–5 add safe DOCX/planning references, director/storyboard editing,
narration/SRT, FFmpeg composition, library/templates/reports, operator monitoring
and encrypted isolated restore. See [current operator handoff](phase5-operations.md)
and [backup recovery](backup-recovery.md). Cloud/storage/voice quality, actual protected-workload
capacity impact and production backup/restore have not been accepted live.
Edge TTS must remain replaceable; evaluate its terms, voice rights and
reliability before commercial use, since no commercial SLA is assumed.
