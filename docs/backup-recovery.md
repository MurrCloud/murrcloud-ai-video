# Studio backup and isolated recovery

The Phase 5 CLI captures one dedicated Studio PostgreSQL database and private
local media into an age-encrypted bundle. It never stops/starts services,
creates/drops a database, installs tools, accesses protected workloads, fetches S3 objects or
changes infrastructure. Disposable rehearsal results do not verify production
backups, off-host custody or unrelated workload recovery.

## Operator prerequisites

Production maintenance requires explicit approval. Manually quiesce only Studio
HTTP writers and generation/narration/rendering workers and let active publishers
finish. Keep protected workloads and unrelated services running. `--quiesced` acknowledges that
operator action; it does not claim the CLI inspected every writer. A provider job
already in flight may still charge: retain its intent and request ID for review.

- Python 3.12+ with this repository installed, `age`, and PostgreSQL 17
  `pg_dump`/`pg_restore` matching the server major. Tools are added only to the CI
  test image; the API/worker images and host are not changed.
- A dedicated URL using `postgresql+psycopg`, explicit host/user and database
  `video_studio` or `video_studio_<suffix>`. Unrelated database names and `postgres` are refused.
  Only optional `sslmode` and `sslrootcert` query settings are accepted.
- Existing owner-only (`0700`) canonical source, staging and archive directories.
  Symlinks and hard-linked files are refused. Production staging/output must be
  on a filesystem separate from `/`, with reviewed quotas and I/O limits. A device
  check alone does not prove isolation or a quota. Protected workloads retain priority.
- An operator-controlled X25519 `age1...` public recipient. Keep its private
  identity offline, outside source/backup paths; rehearse operator access.

No cron, timer, host agent, volume, Docker socket mount, transfer or retention
policy is installed. Run under approved Studio CPU/memory/process/I/O limits.
Default caps: 1 GiB database dump, 2 GiB copied media, 4 GiB archive, 16 MiB
manifest, 100,000 records, 900 seconds/tool and a 15 GiB free-space floor. Preflight
reserves worst-case combined staging/output bytes per filesystem, then watches
growth/free space. External tools have hard file/CPU/open-file limits and a Linux
parent-death guard, with a 2 GiB virtual-memory ceiling distinct from resident
RAM limits. Use filesystem quotas as the aggregate hard bound; a restored database
also needs its own quota. Oversized captures fail; they never silently
evict/omit media. Ephemeral test fixtures lower caps and bypass the device check
only through the Python API. The operator CLI offers no such override.

## Capture

Inject `VIDEO_DATABASE_URL` through the approved secret mechanism. Never place its
value in arguments, shell history, source control or logs. Use reviewed paths and
the actual public recipient in place of these placeholders:

```sh
umask 077
python3 -m app.backup create \
  --database-url-env VIDEO_DATABASE_URL \
  --storage-dir /reviewed-studio-media \
  --staging-dir /reviewed-backup-filesystem/staging \
  --output /reviewed-backup-filesystem/archives/studio-example.tar.age \
  --recipient age1PUBLIC_RECIPIENT_FROM_OPERATOR \
  --quiesced
```

`scripts/backup.sh` wraps this CLI and requires `VIDEO_DATABASE_URL`,
`VIDEO_STORAGE_DIR`, `VIDEO_BACKUP_STAGING_DIR`, `VIDEO_BACKUP_DIR`,
`VIDEO_BACKUP_AGE_RECIPIENT` and `VIDEO_BACKUP_QUIESCED=true`. It never runs service
or Docker commands. Output names must be new.

Retain the JSON report (ciphertext SHA-256, bytes, timestamp and record count)
with the commit/revision in an independently trusted inventory. Transfer only
encrypted archives to approved off-host custody and verify the hash there.
Retention, transfer, deletion, alerts and key custody remain operator work. The
bundle includes no `.env`, identity, provider secret file or unrelated source
file. Database password hashes, sessions, private script text and media are still
sensitive content protected by encryption and restricted custody.

Capture holds the Studio media publication/quota lock and exports a read-only
repeatable-read PostgreSQL snapshot; uncompressed custom-format
`pg_dump --snapshot` uses that same view with a bounded lock wait.
Published media sizes/SHA-256 must match database rows. Ready media must exist;
published pending media is included/checked and absent pending media is recorded
as absent. Only local UUID-keyed media is supported. S3-backed rows, missing ready
objects/checksums, unsafe paths or version/capacity failures abort. Ingested DOCX
script text lives in the database; arbitrary original files, rejected/deleted
media and scratch data are excluded. S3 completeness is not claimed.

Output is exclusively published after encryption, never replacing an existing
archive. Handled failures remove only operation-owned temporary/partial paths.
Abrupt termination can leave private staging/partial remnants; an operator must
inspect them and arrange reviewed cleanup without touching unrelated data. Raw
staging is sensitive; use encrypted storage and reviewed disposal. Secure erasure
is not claimed.

## Isolated restore

Separately create an empty PostgreSQL 17 database from `template0` named
`video_studio_restore_<8-to-64 lowercase letters/digits/underscores>` with isolated
credentials. No API, worker, beat, outbox sweep or broker may connect. The CLI
never creates/drops a database and rejects normal/live names and nonempty targets.
Use an absent media target under a private parent. Staging and that parent must
share one reviewed non-root filesystem for exclusive atomic publication. Use the
same application schema revision and matching PostgreSQL major; automatic
migration/upgrade is refused. Keep existing Studio/unrelated workload credentials inaccessible.

Supply the expected **encrypted archive SHA-256 from a trusted independent
record**, not a sidecar obtained with an untrusted archive. age integrity does
not authenticate the sender: anyone with the public recipient can encrypt a
malicious dump. PostgreSQL restores may execute source SQL. Verify origin before
import. Use an owner-only `0600` X25519 identity; never pass its text or the
database URL value in arguments:

```sh
umask 077
python3 -m app.backup restore \
  --database-url-env VIDEO_RESTORE_DATABASE_URL \
  --archive /approved-custody/studio-example.tar.age \
  --expected-sha256 TRUSTED_64_CHARACTER_CIPHERTEXT_SHA256 \
  --identity /operator-private/recovery-identity.txt \
  --storage-target /reviewed-recovery-filesystem/studio-media-new \
  --staging-dir /reviewed-recovery-filesystem/staging
```

The bounded strict USTAR reader accepts exactly the first-position manifest,
`database.dump` and its UUID media list. Unknown/duplicate members, traversal,
absolute paths, links, directories, devices, PAX/GNU extensions, invalid headers,
nonzero padding, oversized/truncated data and checksum mismatches are rejected.
All content is validated before `pg_restore --single-transaction --exit-on-error
--no-owner --no-acl`. The CLI never uses `--clean` or `--create`. It checks restored
schema/media rows and applies recovery fences:

- Revoke every restored session; require fresh sign-in.
- Clear restored worker heartbeats. Only new recovery-environment heartbeats may
  establish worker presence; source-worker freshness is never evidence of a
  running recovered worker.
- Mark nonterminal video jobs `reconciliation_required`, disable dispatch and set
  `restore_review_required`. Preserve provider IDs, attempt history, reservations,
  approved caps, committed spend and unknown/confirmed charges.
- Fence approved/submitting/ambiguous director calls. Draft plans still
  require explicit paid approval before a call.
- Fail interrupted local renders for review, clear leases, fail active project
  production state and append an isolated-restore audit event.

No worker or external call starts. Fencing does not prove provider cancellation
or absence of charges. Reconcile existing/unknown requests before any release;
do not free reservations just to make budget available. Redis contents are not
restored. Any subsequent environment needs fresh dedicated Studio Redis, never
an old queue with stale tasks. Review pending published-media recovery first.

Media publication uses `renameat2(RENAME_NOREPLACE)` on Linux or
`renamex_np(RENAME_EXCL)` on macOS; there is no overwrite fallback. After failure,
the isolated database may be empty or contain imported data; already published
new media may remain for investigation. Nothing automatically resets/drops it.
Keep it disconnected and use a new operator-arranged destination for another
attempt. Only this operation's temporary paths are automatically cleaned.

## Evidence and release gates

`tests/test_backup.py` covers hostile archives, paths, hashes/caps, non-overwriting
publication, tool bounds and secret-safe errors. `tests/test_backup_runtime.py`
uses real age and PostgreSQL 17 in disposable Compose with
`VIDEO_TEST_BACKUP_ENABLED=true` and `VIDEO_LIVE_DATABASE_URL` restricted to the
`postgres` service's `video_studio` database. It creates only its own random source
and recovery databases, migrates the source, verifies snapshot consistency while
the source changes, round-trips fixture rows/media and checks fencing and retained
paid history. Cleanup drops only databases the test just created. No customer
media, live provider or paid job participates.

Record CI evidence and an approved production-like isolated rehearsal before
release. Check private media access/references, hash inventory, login/session
revocation, unknown cost reconciliation and disabled workers. The owner must
define RPO/RTO, retention, custody and recovery promotion, and rehearse actual
approved storage/backend volumes. Production maintenance, promotion, DNS/proxy/
service changes and protected-workload capacity acceptance require explicit approval. Production
backup/restore and off-host retention are not yet verified.

## Official references

- [PostgreSQL 17 pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html):
  exported snapshots and custom-format dumps.
- [PostgreSQL 17 pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html):
  transactions, error handling and source SQL trust.
- [age upstream](https://github.com/FiloSottile/age) and
  [age manual](https://github.com/FiloSottile/age/blob/main/doc/age.1): X25519
  recipients/identities and sender-authentication limitations.

Untrusted member names are parsed locally and never delegated
to a general archive extraction command.
