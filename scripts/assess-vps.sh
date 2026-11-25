#!/usr/bin/env bash
# Run locally on the reviewed host. Read-only; does not SSH or inspect secrets.
set -euo pipefail
export LC_ALL=C

printf '%s\n' 'Video Studio host assessment (read only)'
uname -srm
python3 - <<'PY'
import json
import os
from pathlib import Path

release = {}
for line in Path('/etc/os-release').read_text().splitlines():
    if '=' in line:
        key, value = line.split('=', 1)
        if key in {'NAME', 'VERSION_ID'}:
            release[key] = value.strip('"')
memory = {}
for line in Path('/proc/meminfo').read_text().splitlines():
    key, value = line.split(':', 1)
    if key in {'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'}:
        memory[key + '_KiB'] = int(value.strip().split()[0])
disk = os.statvfs('/')
print(json.dumps({
    'os': release,
    'cpu_count': os.cpu_count(),
    'memory': memory,
    'load_1_5_15': list(os.getloadavg()),
    'root_total_bytes': disk.f_blocks * disk.f_frsize,
    'root_available_bytes': disk.f_bavail * disk.f_frsize,
}, sort_keys=True, indent=2))
PY

if (( $# == 0 )); then
    printf '%s\n' 'Supply the reviewed protected systemd service names as arguments.' >&2
    exit 1
fi
for unit in "$@"; do
    if [[ ! "$unit" =~ ^[A-Za-z0-9_@.:-]+\.service$ ]]; then
        printf '%s\n' 'Invalid protected service name.' >&2
        exit 1
    fi
    if systemctl is-active --quiet "$unit"; then
        printf '%s active\n' "$unit"
    else
        printf '%s inactive-or-unavailable\n' "$unit"
    fi
done
if command -v docker >/dev/null 2>&1; then
    docker --version
    docker compose version
fi
printf '%s\n' 'No environments, credentials, application records or full proxy configuration were read.'
