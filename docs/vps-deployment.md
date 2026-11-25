# VPS deployment template

These are reusable examples for a separately reviewed host. They establish no
deployment status or authorization. Complete the private [server assessment](server-assessment.md)
and [deployment checklist](deployment-checklist.md) before applying them. Keep
completed host identities, addresses, approvals and evidence outside Git.

Example hostnames use `video.example.test`; paths and the private subnet are
templates and must be reviewed for conflicts. Preserve every unrelated workload,
database, credential, route, filesystem and backup. Production changes require
scoped authorization and a Studio-only rollback. Bootstrap a chosen administrator
using the password-prompt CLI; never commit or log its temporary password.

## Runtime and release contract

Use only `postgres`, `redis`, `api` and `frontend`, with the fixed Compose project
name `murrcloud-video-studio`. Supply files in this order:

1. `docker-compose.yml`
2. `docker-compose.prod.yml`
3. `infrastructure/docker/compose.vps.yml`

The final override requires `VIDEO_BACKEND_IMAGE`, `VIDEO_FRONTEND_IMAGE`,
`VIDEO_POSTGRES_IMAGE` and `VIDEO_REDIS_IMAGE` to identify verified imported images.
Follow [deployment image verification](deployment-images.md), retain the source
SHA and image IDs, and use `pull_policy: never`, `--no-build` and `--pull never`.
Do not run Docker builds or automated suites on the shared host. The example initial
container ceilings are **0.90 CPU / 1280 MiB** in aggregate. These are ceilings,
not resources reserved for protected workloads, and do not account for daemon/image overhead.

All five application gates remain false: paid video, director, narration, local
rendering and S3. Provider keys/approval references are blanked in the VPS API
override. No worker profile is started. Changing these settings later requires a
reviewed change and the applicable authorization, not merely adding an API key.

The administrator's Settings key vault additionally accepts a persistent,
independent `VIDEO_PROVIDER_KEY_ENCRYPTION_KEY`, supplied only to the API.
It encrypts staged database copies; it does not populate runtime provider keys
or change any spending gate. Generate it once privately, retain it across updates,
and follow [key custody and recovery](provider-key-settings.md).

Stage immutable release sources below `/opt/video-studio/releases/`.
The reviewed release is selected by the root-owned `current` link; the systemd
unit works in `/opt/video-studio/current`. Private operator settings are
in `/opt/video-studio/shared/studio.env`, mode `0600`, outside source.
Generate independent Studio database, Redis, session and administrator secrets.
Never print the resolved Compose config or reuse unrelated workload credentials. The shared
environment must quote JSON origins consistently, for example:

```dotenv
VIDEO_ALLOWED_ORIGINS='["https://video.example.test"]'
VIDEO_TRUSTED_PROXY_IPS=127.0.0.1
```

Use production HTTPS origins and Secure cookies. The VPS override uses a dedicated
internal bridge `172.28.100.0/28`, gateway `172.28.100.1`, static API
`172.28.100.2:8100` and frontend `172.28.100.3:3100`. Dynamic database/cache addresses
are restricted to `172.28.100.8/29`, outside those reserved upstream addresses.
Confirm the subnet is unused before creating the Studio network; do not change
existing networks. Pair this override with
`infrastructure/nginx/video.example.test.vps.conf.example`.

The override resets inherited API/frontend published ports with Compose
`!reset []`, requiring a compatible Compose version. Host Nginx reaches the
example private bridge addresses directly. Confirm the target Docker/network
behavior in isolation; no Studio container port is published and the internal
network provides no external egress. The loopback proxy example is a different
configuration and must not be mixed with the private-bridge upstreams.

Before exposing the API, verify its observed Nginx peer and set
`VIDEO_TRUSTED_PROXY_IPS=172.28.100.1` only if it matches the dedicated gateway.
The default trusts only `127.0.0.1`; no wildcard, CIDR or arbitrary forwarded header
is allowed by the systemd preflight. The proxy must overwrite incoming forwarded
headers. Without correct peer trust, rate limits may aggregate clients under the
bridge IP.

## Host identity and protected-workload checks

Both host-side preparation tools require the exact reviewed `/etc/machine-id`
value via `--expected-machine-id` (32 lowercase hexadecimal characters). There is
no default, wildcard or automatic identity discovery; absent, malformed or
mismatching identity refuses work. Obtain the expected value through an
independently authenticated private assessment, not from an untrusted payload.

`scripts/prepare-vps-storage.py` also requires `--protected-service UNIT.service`
for each protected systemd service and `--protected-listener-port PORT` for the
reviewed application listener. It verifies root/host identity, free capacity,
protected-service health, absent Studio paths and an unused mount unit. Without
`--create-new` it performs preflight only. The mutation flag creates only new
operation-owned paths and bounded storage; it is never an update or repair tool.

`scripts/fetch-deployment-artifact.py` additionally requires `--expected-repository owner/name`. That explicitly trusted repository must match the manifest origin;
payload-provided identity is not trusted configuration. Follow its bounded,
checksum-verified input contract and [image guide](deployment-images.md).

`scripts/assess-vps.sh` requires the protected `.service` units as positional
arguments. The host signal collector requires explicit `--erp-unit`,
`--postgres-unit`, `--nginx-unit` and an HTTPS `--erp-health-url`; these legacy
option names describe protected-workload roles, not a particular host inventory.
Probe failures, redirects, missing measurements and stale signals deny admission.
See `infrastructure/monitoring/README.md`. Keep probe addresses and observations
private; never install synthetic CI signals on a real deployment.

## Bounded Studio storage

All three persistent data paths must be inside a separately prepared **8 GiB
fixed-size ext4 loop filesystem** mounted at `/srv/video-studio`. Its
backing file is `/var/lib/video-studio/data.ext4`. Prepare only these new
Studio paths; do not remount root, change unrelated filesystems, mount unrelated directories or
modify an unrelated database. No filesystem-creation or mount unit is installed by
this repository example. The operator prepares and verifies that unit separately.

Local-driver named volumes bind exactly these directories:

| Volume | Host path | Required owner |
| --- | --- | --- |
| video_postgres | /srv/video-studio/postgres | 999:999, private |
| video_redis | /srv/video-studio/redis | 999:999, private |
| video_storage | /srv/video-studio/media | 10001:10001, private |

Verify the actual mounted filesystem UUID, type, size and loop backing file;
check the expected host identity and retain their nonsecret inventory. Create a
root-controlled `.studio-filesystem` marker on that mounted filesystem, containing
the reviewed filesystem/host identity. The service checks mountpoint and marker
presence, which does not itself authenticate the UUID or backing file. Before
enabling it, verify those values against the inventory. Never silently reuse an
existing named volume with different driver options or existing unrelated data.

The backing file must be fully allocated before use, rather than allowing sparse
growth to consume the unrelated filesystem later. Check retained root free space first.
The 8 GiB bound is shared by Studio PostgreSQL, Redis and media: it is not 8 GiB per
volume or an additional per-volume reservation. Application media limits and its
free-space floor still apply; database/cache can consume their shared filesystem.
Monitor free space/inodes and preserve enough recovery headroom. Exhaustion should
fail Studio writes within its own filesystem, rather than consuming all root
space; shared I/O, memory and host failure risks still require monitoring.

The paired VPS Nginx route stages request bodies at the always-existing host path
`/var/cache/video-studio/nginx-body`, independently of the Studio data
filesystem. Precreate the parent root-owned `0755` and the underlying body
directory owned by `www-data` (`33:33`), mode `0700`. Install the reviewed
`infrastructure/systemd/studio-nginx-body.mount.example` under the exact name from
`systemd-escape --path --suffix=mount /var/cache/video-studio/nginx-body`:
Use the exact output from that command; do not guess the escaped unit name.

This Studio-only tmpfs caps body staging at 16 MiB with `nodev,nosuid,noexec`,
mode `0700` and owner `33:33`. Its memory use is outside the container ceilings.
Enable only this new mount unit. Do not add dependencies to shared Nginx or the
Studio data filesystem: shared Nginx must start/reload even if Studio storage is
unavailable. Both proxy locations disable request buffering and explicitly use
HTTP/1.1 so chunked request bodies can stream too; the 6 MiB proxy body limit and
stricter backend limits remain enforced.

If the tmpfs fails or is unmounted, Nginx can use the precreated underlying
directory to preserve shared-proxy availability. In that fallback, the 16 MiB bound
does not apply to files on root; monitor the mount and root free space, and put
only the Studio route into maintenance if needed. Streaming reduces staging but
is not a filesystem quota. Do not treat the Studio data filesystem's 8 GiB bound
as a limit on Nginx, logs or Docker image storage.

The optional ACME webroot `/srv/video-studio/acme` is root-controlled,
readable by the Nginx worker and outside all application volume binds. Its
absence can fail a Studio certificate challenge without making shared Nginx
depend on the Studio data mount. Prepare the exact Studio-only route and preserve
all unrelated routes and filestore paths.

## Mount-guarded startup

Review `infrastructure/systemd/murrcloud-video-studio.service.example` against the
host's Docker/Python paths. It requires the dedicated mount, then checks its
mountpoint/marker, exact proxy addresses and quiet Compose validity. It starts
only the four named services with imported images and waits at most 180 seconds
for health. It never installs a mount unit, changes DNS, restarts Docker, or
starts/restarts protected workloads, unrelated database services or Nginx.

The VPS override deliberately sets Docker `restart: "no"` on these four services.
Docker daemon auto-start must not bypass the systemd mount check on reboot and
bind data directories on the underlying root filesystem. The Studio systemd unit
owns ordered startup/shutdown; it has no automatic restart loop. A failed or
unhealthy container needs operator inspection and a reviewed Studio-only restart.
`RemainAfterExit=yes` means an active systemd unit is not continuous container
health evidence: inspect Docker health and API readiness as well.

During initial bootstrap, start only the dedicated database/cache using these
same verified files and image references, after the storage/marker checks.
Migrate only `video_studio` to schema head `0006`, seed the model catalog offline
and create the administrator through the password-prompt CLI. Never migrate the
unrelated database, copy unrelated workload credentials, or expose database/cache ports. Provide the
new administrator credential privately, without command-line values or logs.
Then use the reviewed service to start the full four-service initial stack.

Production Compose also requires an existing private `VIDEO_HOST_SIGNAL_FILE`.
With every worker/remote feature disabled, a deliberately stale/invalid
Studio-only signal can document denied admission until the approved collector is
installed. Do not invent fresh healthy host or protected-workload measurements. API/database/UI
can run independently; missing or unhealthy admission keeps production work off.

## Verification and Studio-only rollback

Capture protected-service state, process start identities, existing container
identities and reviewed read-only health status/latency before and after changes.
Do not create business records in unrelated applications as a deployment test. Healthy read-only probes
are limited evidence, not a claim that every protected business workflow has been exercised.

Verify Studio readiness/schema, HTTPS login/cookie attributes, project persistence
and catalog access. Confirm all five feature gates false, no worker containers
or generation/director/render jobs, the dedicated internal network, fixed private
API/frontend upstreams and no host-published ports on any Studio container.
Inspect actual image IDs, security
flags, resources, bound volume paths and the mounted filesystem identity.

Use `systemctl stop murrcloud-video-studio` for the reviewed Studio-only stop.
Its commands stop only this Compose project's four services and preserve volumes;
partial startup failure also attempts a Studio-only cleanup. Verify resulting
container states. Never run `down --volumes`, automatic Alembic downgrade,
unrelated Docker pruning or protected application/database/Docker-daemon restart. Do not
unmount the filesystem while Studio containers still use its bind volumes.

Preserve Studio data and use a schema-compatible reviewed image rollback if
needed. Backups and [isolated recovery](backup-recovery.md) need their own custody,
retention and tested procedure; an 8 GiB loop filesystem is not a backup. A later
server-plan upgrade is a separate user decision. No paid generation is part of
this deployment or verification.

## Primary references reviewed

- [Docker automatic starts](https://docs.docker.com/engine/containers/start-containers-automatically/)
  distinguishes daemon-start restart policies from host-managed lifecycle.
- [Compose service configuration](https://docs.docker.com/reference/compose-file/services/)
  documents image, command, pull and restart behavior.
- [Uvicorn settings](https://uvicorn.dev/settings/) describes exact proxy trust.
- [Nginx request buffering](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_request_buffering)
  documents streaming and the HTTP/1.1 requirement for chunked bodies.
- [systemd unit dependencies](https://freedesktop.org/software/systemd/man/devel/systemd.unit.html)
  documents `RequiresMountsFor`.
- [systemd service lifecycle](https://www.freedesktop.org/software/systemd/man/254/systemd.service.html)
  documents oneshot, preflight, retained-active state and stop processing.

Actual deployment and environment acceptance must be recorded privately
with its exact source/image identities; these examples alone are not live proof.
