#!/usr/bin/env python3
"""Read host health and update one nonsecret signal; never changes host services."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def health_target(value: str) -> str:
    """Accept only an operator-supplied HTTPS probe, without credentials or redirects."""
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and not any(character.isspace() for character in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("Use a reviewed HTTPS health URL without credentials.")
    return value


def active(unit: str) -> bool:
    try:
        return (
            subprocess.run(
                ["systemctl", "is-active", "--quiet", unit],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=4,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--erp-unit", required=True)
    parser.add_argument("--postgres-unit", required=True)
    parser.add_argument("--nginx-unit", required=True)
    parser.add_argument("--erp-health-url", required=True, type=health_target)
    args = parser.parse_args()
    if any(
        not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", unit)
        for unit in (args.erp_unit, args.postgres_unit, args.nginx_unit)
    ):
        raise SystemExit("Invalid protected service unit.")
    output = args.output
    if not output.is_absolute() or output.parent.resolve() != output.parent:
        raise SystemExit("Use an absolute signal path without linked directories.")
    parent = output.parent.stat()
    if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
        raise SystemExit("Signal directory must belong to this operator and deny other writes.")
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, value = line.split(":", 1)
        memory[name] = int(value.strip().split()[0]) * 1024
    disk = os.statvfs("/")
    http_status = None
    response_ms = None
    began = time.monotonic()
    try:
        # Explicit reviewed target; no redirects, proxies, cookies or response logging.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(args.erp_health_url, timeout=3) as response:
            http_status = response.status
            response_ms = round((time.monotonic() - began) * 1000, 3)
    except (OSError, urllib.error.URLError, ValueError):
        pass
    signal = {
        "schema_version": 1,
        "observed_at": dt.datetime.now(dt.UTC).isoformat(),
        "memory_available_bytes": memory["MemAvailable"],
        "root_free_bytes": disk.f_bavail * disk.f_frsize,
        "cpu_count": os.cpu_count() or 1,
        "load1": os.getloadavg()[0],
        "erp_http_status": http_status,
        "erp_response_ms": response_ms,
        "services": {
            "erp": active(args.erp_unit),
            "postgresql": active(args.postgres_unit),
            "nginx": active(args.nginx_unit),
        },
    }
    encoded = (json.dumps(signal, sort_keys=True) + "\n").encode()
    descriptor = os.open(output, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)
    with os.fdopen(descriptor, "r+b") as destination:
        info = os.fstat(destination.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise SystemExit("Unsafe signal file ownership, permissions or type.")
        fcntl.flock(destination.fileno(), fcntl.LOCK_EX)
        # Preserve this inode: production containers bind this single file.
        destination.seek(0)
        destination.write(encoded)
        destination.truncate()
        destination.flush()
        os.fsync(destination.fileno())
    print("Host signal refreshed.")


if __name__ == "__main__":
    main()
