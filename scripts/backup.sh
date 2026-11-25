#!/usr/bin/env bash
# Explicit operator capture only. This wrapper never stops/restarts any service.
set -euo pipefail
umask 077
: "${VIDEO_DATABASE_URL:?Set a dedicated Video Studio database URL}"
: "${VIDEO_STORAGE_DIR:?Set the independent Video Studio storage directory}"
: "${VIDEO_BACKUP_DIR:?Set an existing private directory on reviewed separate storage}"
: "${VIDEO_BACKUP_STAGING_DIR:?Set an existing private staging directory on reviewed separate storage}"
: "${VIDEO_BACKUP_AGE_RECIPIENT:?Set the operator-controlled age public recipient}"
if [[ "${VIDEO_BACKUP_QUIESCED:-}" != "true" ]]; then
    printf '%s\n' 'Refused: quiesce only studio writers under an approved plan, then set VIDEO_BACKUP_QUIESCED=true.' >&2
    exit 1
fi
stamp=$(date -u '+%Y%m%dT%H%M%SZ')
exec python3 -m app.backup create \
    --database-url-env VIDEO_DATABASE_URL \
    --storage-dir "$VIDEO_STORAGE_DIR" \
    --output "$VIDEO_BACKUP_DIR/video-studio-$stamp.tar.age" \
    --recipient "$VIDEO_BACKUP_AGE_RECIPIENT" \
    --staging-dir "$VIDEO_BACKUP_STAGING_DIR" --quiesced
