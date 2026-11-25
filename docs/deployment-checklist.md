# Controlled deployment checklist

This is an uncompleted template, not a deployment record or authorization.
Keep completed inventories, addresses, approvals and evidence in private operator
storage outside the repository. See [server assessment](server-assessment.md),
[VPS deployment design](vps-deployment.md), [image verification](deployment-images.md)
and [backup/recovery](backup-recovery.md).

- [ ] Record the exact authorized host, source commit, scope and rollback privately.
- [ ] Complete a fresh host assessment and protected-workload baseline.
- [ ] Verify independent recovery points and an isolated restore rehearsal.
- [ ] Pass current isolated backend/frontend, migration and container tests.
- [ ] Verify runtime image provenance, identities, architecture and non-root user.
- [ ] Prepare dedicated bounded storage, marker, ownership and host identity checks.
- [ ] Generate independent database/cache/session/admin credentials privately.
- [ ] Retain the API-only provider-key encryption master outside backup custody.
- [ ] Validate quiet Compose configuration without printing resolved secrets.
- [ ] Start only dedicated database/cache; explicitly migrate the Studio database.
- [ ] Seed catalog metadata offline and bootstrap the administrator by password prompt.
- [ ] Start only API/frontend with paid video/director/narration/render/S3 disabled.
- [ ] Verify fresh readiness, authentication, ownership, persistence and disabled gates.
- [ ] Review any DNS/TLS/proxy change separately and preserve every unrelated route.
- [ ] Compare protected-workload health before/during/after the scoped rollout.
- [ ] Retain a schema-compatible Studio-only rollback and private result record.

A read-only health probe does not prove all business workflows or capacity.
Never use a live shared host to run automated suites, Docker builds or synthetic
acceptance workloads. Stop Studio work on unsafe host conditions; preserve its
volumes and paid-intent records. Do not restart unrelated services, prune their
images, run production `down --volumes` or automatically downgrade a schema.
