#!/usr/bin/env python3
"""Read-only verification of applied container isolation in the disposable CI stack."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT = "murrcloud-video-ci"
ROOT = Path(__file__).resolve().parents[1]
NETWORK = f"{PROJECT}_video_internal"
MIB = 1024**2
LIMITS = {
    "api": (350_000_000, 384 * MIB, 96),
    "postgres": (350_000_000, 512 * MIB, 96),
    "redis": (150_000_000, 128 * MIB, 32),
    "frontend": (150_000_000, 256 * MIB, 64),
    "generation-worker": (150_000_000, 256 * MIB, 64),
    "rendering-worker": (250_000_000, 512 * MIB, 64),
}
VOLUMES = {
    "api": {"/var/lib/video": "video_storage"},
    "postgres": {"/var/lib/postgresql/data": "video_postgres"},
    "redis": {"/data": "video_redis"},
    "frontend": {},
    "generation-worker": {"/var/lib/video": "video_storage"},
    "rendering-worker": {"/var/lib/video": "video_storage"},
}
REDIS_BINDS = {
    "/opt/video/redis-entrypoint.sh": ROOT / "infrastructure/docker/redis-entrypoint.sh",
    "/opt/video/redis-health.sh": ROOT / "infrastructure/docker/redis-health.sh",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def docker(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["docker", *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Docker inspection could not complete") from exc
    # Never echo stdout/stderr: inspect contains environment secrets.
    require(result.returncode == 0, "Docker inspection failed")
    return result.stdout


def inspect(kind: str, identity: str) -> dict:
    try:
        data = json.loads(docker("inspect", "--type", kind, identity))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Docker inspection returned invalid metadata") from exc
    require(
        isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict),
        "Docker inspection returned unexpected metadata",
    )
    return data[0]


def check_ports(service: str, container: dict) -> None:
    host = container["HostConfig"]
    require(not host.get("PublishAllPorts"), f"{service}: automatic port publication enabled")
    for bindings in (
        host.get("PortBindings") or {},
        container["NetworkSettings"].get("Ports") or {},
    ):
        for port, publications in bindings.items():
            if not publications:
                continue
            require(
                service == "api" and port == "8100/tcp", f"{service}: unexpected published port"
            )
            for publication in publications:
                try:
                    loopback = ipaddress.ip_address(publication.get("HostIp", "")).is_loopback
                except ValueError:
                    loopback = False
                require(loopback, "api: published address is not loopback")


def check_mounts(service: str, container: dict) -> None:
    expected = VOLUMES[service]
    found = set()
    for mount in container.get("Mounts", []):
        destination = mount.get("Destination", "")
        kind = mount.get("Type")
        if kind == "volume":
            require(destination in expected, f"{service}: unexpected persistent mount")
            name = f"{PROJECT}_{expected[destination]}"
            require(mount.get("Name") == name, f"{service}: persistent volume is not CI isolated")
            volume = inspect("volume", name)
            require(
                volume.get("Driver") == "local" and not volume.get("Options"),
                f"{service}: volume has external driver or host mount options",
            )
            require(
                (volume.get("Labels") or {}).get("com.docker.compose.project") == PROJECT,
                f"{service}: persistent volume belongs to another project",
            )
            found.add(destination)
        elif kind == "bind":
            require(
                service == "redis" and destination in REDIS_BINDS,
                f"{service}: unexpected host bind (unrelated host/socket mounts forbidden)",
            )
            require(
                Path(mount.get("Source", "")).resolve() == REDIS_BINDS[destination],
                "redis: host bind does not match the reviewed repository script",
            )
            require(mount.get("RW") is False, "redis: repository script mount is writable")
        elif kind == "tmpfs":
            require(
                destination in (container["HostConfig"].get("Tmpfs") or {}),
                f"{service}: unconfigured temporary mount",
            )
        else:
            raise RuntimeError(f"{service}: unexpected mount type")
    require(found == set(expected), f"{service}: required dedicated volume missing")


def check_container(service: str) -> None:
    identities = docker("compose", "ps", "-q", service).split()
    require(
        len(identities) == 1 and bool(re.fullmatch(r"[0-9a-f]{12,64}", identities[0])),
        f"{service}: expected exactly one running container",
    )
    container = inspect("container", identities[0])
    labels = container["Config"].get("Labels") or {}
    require(
        labels.get("com.docker.compose.project") == PROJECT
        and labels.get("com.docker.compose.service") == service,
        f"{service}: container identity is outside the disposable CI project",
    )
    require(container["State"].get("Running") is True, f"{service}: container is not running")
    user = container["Config"].get("User", "")
    require(
        bool(re.fullmatch(r"[1-9][0-9]*(?::[1-9][0-9]*)?", user)),
        f"{service}: applied user is not an explicit non-root UID",
    )
    host = container["HostConfig"]
    require(host.get("ReadonlyRootfs") is True, f"{service}: root filesystem is writable")
    require(not host.get("Privileged"), f"{service}: privileged container")
    require(
        "ALL" in (host.get("CapDrop") or []) and not host.get("CapAdd"),
        f"{service}: capabilities are not fully dropped",
    )
    require(
        any(
            value in {"no-new-privileges", "no-new-privileges:true"}
            for value in host.get("SecurityOpt") or []
        ),
        f"{service}: no-new-privileges is missing",
    )
    cpu_max, memory_max, pids_max = LIMITS[service]
    nano_cpus = host.get("NanoCpus") or 0
    if not nano_cpus:
        quota, period = host.get("CpuQuota") or 0, host.get("CpuPeriod") or 0
        require(quota > 0 and period > 0, f"{service}: CPU quota is missing")
        nano_cpus = quota * 1_000_000_000 / period
    require(0 < nano_cpus <= cpu_max, f"{service}: CPU ceiling is missing or too high")
    require(
        0 < (host.get("Memory") or 0) <= memory_max,
        f"{service}: memory ceiling is missing or too high",
    )
    require(
        0 < (host.get("PidsLimit") or 0) <= pids_max,
        f"{service}: process ceiling is missing or too high",
    )
    require(
        set(container["NetworkSettings"].get("Networks") or {}) == {NETWORK},
        f"{service}: container joins an unexpected network",
    )
    check_ports(service, container)
    check_mounts(service, container)


def main() -> int:
    require(
        os.environ.get("COMPOSE_PROJECT_NAME") == PROJECT,
        "Refusing inspection outside the explicit murrcloud-video-ci project",
    )
    for service in LIMITS:
        check_container(service)
    network = inspect("network", NETWORK)
    require(network.get("Internal") is True, "CI network is not internal")
    require(
        (network.get("Labels") or {}).get("com.docker.compose.project") == PROJECT,
        "CI network belongs to another project",
    )
    print("Applied CI container isolation verified for frontend, API, PostgreSQL and Redis.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        # RuntimeError messages are controlled literals, never raw Docker output.
        print(f"Container isolation verification failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except (KeyError, TypeError, ValueError):
        # Keep diagnostics free of raw Docker metadata/environment values.
        print(
            "Container isolation verification failed; inspect the CI configuration.",
            file=sys.stderr,
        )
        sys.exit(1)
