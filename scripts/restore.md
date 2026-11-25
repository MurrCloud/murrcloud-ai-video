# Explicit isolated Studio recovery

Use the tested Python CLI and follow [the backup/recovery runbook](../docs/backup-recovery.md).
Do not restore with a bare tar command, `pg_restore --clean`, a protected workload connection or
a live Studio URL. The CLI requires a trusted independent encrypted archive
SHA-256, private age identity, empty `video_studio_restore_*` database and absent
local media target. It never creates/drops databases, stops/starts services,
fetches S3 objects, replays paid jobs or modifies infrastructure.

Recovery stays isolated, with workers disabled and fresh dedicated Redis.
Restored sessions are revoked; paid intents, reservations and unknown charges
are preserved for explicit reconciliation. Use a new recovery target after a
failed attempt; existing targets are never automatically reset.

Production maintenance, retention/custody, promotion and DNS/proxy/service
changes require explicit approval. Production backup and protected workload restore have not
been verified by these automated tests.
