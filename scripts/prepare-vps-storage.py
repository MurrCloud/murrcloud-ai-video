#!/usr/bin/env python3
"""Create a new bounded Studio filesystem only on an explicitly reviewed host."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path("/var/lib/video-studio")
MOUNT = Path("/srv/video-studio")
APP = Path("/opt/video-studio")
SIZE = 8 * 1024**3
RESERVE = 15 * 1024**3


def run(*args, timeout=30):
    return subprocess.check_output(args, text=True, timeout=timeout).strip()


def validate_host(expected_machine_id=None, protected_services=()):
    if not isinstance(expected_machine_id, str) or not re.fullmatch(
        r"[0-9a-f]{32}", expected_machine_id
    ):
        raise SystemExit("Refused: explicit expected machine identity required")
    if not protected_services or any(
        not re.fullmatch(r"[A-Za-z0-9_@.:-]+\.service", unit) for unit in protected_services
    ):
        raise SystemExit("Refused: explicit protected service inventory required")
    if os.geteuid() != 0 or Path("/etc/machine-id").read_text().strip() != expected_machine_id:
        raise SystemExit("Refused: reviewed root/host identity required")
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    if int(memory["MemAvailable"].split()[0]) * 1024 < 3 * 1024**3:
        raise SystemExit("Refused: insufficient available host memory")
    disk = os.statvfs("/")
    if disk.f_bavail * disk.f_frsize < SIZE + RESERVE + 4 * 1024**3:
        raise SystemExit("Refused: insufficient root free-space reserve")
    for unit in protected_services:
        if run("systemctl", "is-active", unit) != "active":
            raise SystemExit("Refused: protected service unavailable")
    for path in (ROOT, MOUNT, APP):
        if path.exists() or path.is_symlink() or path.parent.resolve() != path.parent:
            raise SystemExit("Refused: Studio path already exists or parent is linked")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-new", action="store_true")
    parser.add_argument(
        "--expected-machine-id", required=True, help="Reviewed /etc/machine-id value"
    )
    parser.add_argument(
        "--protected-service",
        action="append",
        required=True,
        help="Protected systemd service; repeat for every protected workload",
    )
    parser.add_argument(
        "--protected-listener-port",
        type=int,
        required=True,
        help="Protected application TCP listener, between 1 and 65535",
    )
    args = parser.parse_args()
    if not 1 <= args.protected_listener_port <= 65535:
        parser.error("--protected-listener-port must be between 1 and 65535")
    validate_host(args.expected_machine_id, args.protected_service)
    unit_name = run("systemd-escape", "--path", "--suffix=mount", str(MOUNT))
    unit_path = Path("/etc/systemd/system") / unit_name
    if unit_path.exists() or unit_path.is_symlink():
        raise SystemExit("Refused: mount unit already exists")
    if not args.create_new:
        print(
            json.dumps(
                {"preflight": "ready", "image_bytes": SIZE, "mount": str(MOUNT), "unit": unit_name}
            )
        )
        return
    baseline = {
        "units": {
            unit: run(
                "systemctl",
                "show",
                unit,
                "--property=ActiveState,SubState,MainPID,ActiveEnterTimestamp,ExecMainStartTimestamp",
                "--no-pager",
            )
            for unit in args.protected_service
        },
        "protected_containers": run(
            "docker", "ps", "--format", "{{.ID}} {{.Names}} {{.Status}}"
        ).splitlines(),
        "protected_proxy_files": {},
        "protected_listener": run(
            "ss", "-H", "-ltnp", "sport", "=", f":{args.protected_listener_port}"
        ),
    }
    for folder in (Path("/etc/nginx/sites-enabled"), Path("/etc/nginx/conf.d")):
        for path in folder.iterdir():
            if path.is_file():
                baseline["protected_proxy_files"][str(path)] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    for pid in set(re.findall(r"pid=(\d+)", baseline["protected_listener"])):
        raw = Path(f"/proc/{pid}/stat").read_text()
        baseline.setdefault("protected_process_start_ticks", {})[pid] = raw.rsplit(")", 1)[
            1
        ].split()[19]
    # All following files/paths are new and operation-owned. No existing device,
    # root mount, fstab entry, protected path, database or service configuration is edited.
    ROOT.mkdir(mode=0o700)
    APP.mkdir(mode=0o700)
    (APP / "shared").mkdir(mode=0o700)
    (APP / "releases").mkdir(mode=0o700)
    with (APP / "shared" / "predeployment.json").open("x") as target:
        target.write(json.dumps(baseline, sort_keys=True, indent=2) + "\n")
    os.chmod(APP / "shared" / "predeployment.json", 0o600)
    MOUNT.mkdir(mode=0o755)
    image = ROOT / "data.ext4"
    descriptor = os.open(image, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    run("fallocate", "--length", str(SIZE), "--", str(image))
    info = image.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != SIZE:
        raise SystemExit("Refused: unexpected new image type/size")
    run(
        "ionice",
        "-c",
        "3",
        "nice",
        "-n",
        "19",
        "mkfs.ext4",
        "-q",
        "-F",
        "-E",
        "nodiscard,lazy_itable_init=0,lazy_journal_init=0",
        "-m",
        "1",
        "-L",
        "murrcloud_video",
        str(image),
        timeout=120,
    )
    if image.stat().st_blocks * 512 < SIZE:
        raise SystemExit("Refused: initial backing-file space reservation was lost")
    filesystem_uuid = run("blkid", "-s", "UUID", "-o", "value", str(image))
    content = f"""[Unit]
Description=Dedicated bounded MurrCloud Video Studio data filesystem
ConditionPathExists={image}
RequiresMountsFor={image}

[Mount]
What={image}
Where={MOUNT}
Type=ext4
Options=loop,nodev,nosuid,noexec
TimeoutSec=30

[Install]
WantedBy=multi-user.target
"""
    with unit_path.open("x") as target:
        target.write(content)
    os.chmod(unit_path, 0o644)
    run("systemd-analyze", "verify", str(unit_path))
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", unit_name, timeout=60)
    if run("findmnt", "-n", "-o", "UUID", "--target", str(MOUNT)) != filesystem_uuid:
        raise SystemExit("Refused: mounted filesystem identity mismatch")
    if image.stat().st_blocks * 512 < SIZE:
        raise SystemExit("Refused: mounted backing-file space reservation was lost")
    marker = {
        "format": "murrcloud-video-storage-v1",
        "machine_id": args.expected_machine_id,
        "filesystem_uuid": filesystem_uuid,
        "image_bytes": SIZE,
        "mount_unit": unit_name,
    }
    with (MOUNT / ".studio-filesystem").open("x") as target:
        target.write(json.dumps(marker, sort_keys=True) + "\n")
    os.chmod(MOUNT / ".studio-filesystem", 0o600)
    for name, uid in (("postgres", 999), ("redis", 999), ("media", 10001), ("staging", 0)):
        path = MOUNT / name
        path.mkdir(mode=0o700)
        os.chown(path, uid, uid)
    for name in ("acme", "nginx"):
        (MOUNT / name).mkdir(mode=0o755)
    body = MOUNT / "nginx" / "client_body"
    body.mkdir(mode=0o700)
    os.chown(body, 33, 33)
    print(json.dumps({"storage": "created", **marker}, sort_keys=True))


if __name__ == "__main__":
    main()
