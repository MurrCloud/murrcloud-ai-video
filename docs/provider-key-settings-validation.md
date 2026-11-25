# Provider-key Settings verification checklist

This checklist covers the sanitized feature, not a deployed instance. Historical
private runs, image identities, backups and browser evidence are intentionally
absent. Record current results for the staging commit.

## Implemented behavior

Administrators can add/replace/remove staged OpenAI and fal keys with current
password verification. Editors/viewers cannot manage keys. Inputs are masked and
cleared; responses/audits never expose values, fragments, passwords or ciphertext.
Actor/session locks, expected revisions and rate limits fence concurrent changes.
Uncertain outcomes refresh metadata without automatically repeating mutations.

Migration `0006` stores provider-bound authenticated Fernet ciphertext. The
independent `VIDEO_PROVIDER_KEY_ENCRYPTION_KEY` is API-only and private, retained
across updates. Missing master leaves key changes unavailable; malformed values
fail configuration. Saved keys stay inactive, and saving/status refresh makes no
provider request or spending/worker enablement. Removing a saved copy does not
revoke it upstream or remove an independently configured runtime key.

Isolated restore clears ciphertext, advances even empty tombstone revisions and
resets modification metadata. Re-enter keys and obtain fresh spending authorization
after recovery. The master must remain separate from database/media backup custody.

## Checks

- [ ] Backend encrypted storage, provider binding, no leakage and missing-master failure.
- [ ] Administrator reauthentication, active-session locks, revocation and rate limits.
- [ ] Expected revisions and real PostgreSQL concurrent writers.
- [ ] Restore clearing/tombstone fencing and safe error/audit behavior.
- [ ] Frontend masked/cleared fields, conflict refresh, roles and no paid test controls.
- [ ] Full backend/frontend, lint/format, types and production build.
- [ ] Isolated schema lifecycle, live API/proxy/restart/outage and container controls.

Automated transports block unmocked provider calls. Tests neither validate a
customer key nor establish live quality, billing or production capacity.
Use [the Settings guide](provider-key-settings.md) for key custody. Any rollback
must retain a backend compatible with schema `0006` and preserve the independent
master. Withholding the master disables key management; keep its protected copy.
Never automatically downgrade/drop the table or discard the master.
