#!/usr/bin/env python3
"""Receive a short-lived authenticated artifact URL over stdin; never log it."""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path("/srv/video-studio")
MAXIMUM = 2 * 1024**3


def require(value):
    if not value:
        raise ValueError("artifact_verification_failed")


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024**2):
            value.update(chunk)
    return value.hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def verify_url(url):
    parsed = urllib.parse.urlsplit(url)
    require(
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and parsed.port in {None, 443}
    )
    require(
        bool(parsed.hostname)
        and (
            parsed.hostname.endswith(".blob.core.windows.net")
            or parsed.hostname.endswith(".actions.githubusercontent.com")
        )
    )
    require(
        all(
            ipaddress.ip_address(item[4][0]).is_global
            for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        )
    )


def validate_host(expected_machine_id=None):
    require(
        isinstance(expected_machine_id, str) and re.fullmatch(r"[0-9a-f]{32}", expected_machine_id)
    )
    require(os.geteuid() == 0 and ROOT.resolve() == ROOT)
    require(Path("/etc/machine-id").read_text().strip() == expected_machine_id)


def validate_repository(expected_repository):
    require(
        isinstance(expected_repository, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", expected_repository)
        and all(part not in {".", ".."} for part in expected_repository.split("/"))
    )


def validate_manifest_origin(manifest, payload, expected_repository):
    validate_repository(expected_repository)
    require(
        manifest["source_sha"] == payload["source_sha"]
        and manifest["repository"] == expected_repository
    )
    require(
        manifest["workflow_run_id"] == str(payload["run_id"])
        and manifest["platform"] == "linux/amd64"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-machine-id", required=True, help="Reviewed /etc/machine-id value"
    )
    parser.add_argument("--expected-repository", required=True, help="Reviewed owner/repository")
    args = parser.parse_args()
    validate_host(args.expected_machine_id)
    validate_repository(args.expected_repository)
    os.umask(0o077)
    payload = json.loads(sys.stdin.buffer.read(16385))
    require(re.fullmatch(r"[0-9a-f]{40}", payload["source_sha"]))
    require(re.fullmatch(r"[0-9a-f]{64}", payload["artifact_sha256"]))
    require(re.fullmatch(r"[0-9a-f]{64}", payload["manifest_sha256"]))
    require(0 < payload["artifact_size"] <= MAXIMUM)
    require((ROOT / ".studio-filesystem").is_file())
    disk = os.statvfs(ROOT)
    require(disk.f_bavail * disk.f_frsize > 2 * payload["artifact_size"] + 1024**3)
    folder = ROOT / "staging" / ("images-" + payload["source_sha"])
    folder.mkdir(mode=0o700)
    archive = folder / "artifact.zip"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    url, response = payload["url"], None
    for _ in range(4):
        verify_url(url)
        try:
            response = opener.open(url, timeout=30)
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise ValueError("artifact_download_failed") from None
            url = urllib.parse.urljoin(url, exc.headers["Location"])
    require(response is not None and response.status == 200)
    started, size, checksum = time.monotonic(), 0, hashlib.sha256()
    with response, archive.open("xb") as target:
        while chunk := response.read(1024**2):
            size += len(chunk)
            require(size <= payload["artifact_size"] and time.monotonic() - started <= 600)
            target.write(chunk)
            checksum.update(chunk)
        target.flush()
        os.fsync(target.fileno())
    require(size == payload["artifact_size"] and checksum.hexdigest() == payload["artifact_sha256"])
    limits = {"studio-images.tar.gz": MAXIMUM, "manifest.json": 65536, "SHA256SUMS": 2048}
    with zipfile.ZipFile(archive) as bundle:
        require(
            len(bundle.infolist()) == 3
            and {member.filename for member in bundle.infolist()} == set(limits)
        )
        for member in bundle.infolist():
            require(
                not member.is_dir()
                and not member.flag_bits & 1
                and 0 < member.file_size <= limits[member.filename]
            )
            require(stat.S_IFMT(member.external_attr >> 16) in {0, stat.S_IFREG})
            copied = 0
            with bundle.open(member) as source, (folder / member.filename).open("xb") as target:
                while chunk := source.read(1024**2):
                    copied += len(chunk)
                    require(copied <= member.file_size)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            require(copied == member.file_size)
    manifest_file = folder / "manifest.json"
    require(digest(manifest_file) == payload["manifest_sha256"])
    manifest = json.loads(manifest_file.read_text())
    validate_manifest_origin(manifest, payload, args.expected_repository)
    expected = {
        "api": "murrcloud-video-backend:deploy-" + payload["source_sha"],
        "frontend": "murrcloud-video-frontend:deploy-" + payload["source_sha"],
        "postgres": "postgres:17-bookworm",
        "redis": "redis:7.4-alpine",
    }
    require(
        len(manifest["images"]) == 4
        and {image["service"]: image["reference"] for image in manifest["images"]} == expected
    )
    for image in manifest["images"]:
        require(
            re.fullmatch(r"sha256:[0-9a-f]{64}", image["image_id"])
            and image["os"] == "linux"
            and image["architecture"] == "amd64"
        )
        if image["service"] in {"api", "frontend"}:
            require(image["revision"] == payload["source_sha"])
    inner = folder / "studio-images.tar.gz"
    require(
        inner.stat().st_size == manifest["bundle"]["size_bytes"]
        and 0 < manifest["bundle"]["uncompressed_bytes"] <= 4 * 1024**3
    )
    require(digest(inner) == manifest["bundle"]["sha256"])
    print(
        json.dumps(
            {
                "artifact_verified": True,
                "source_sha": manifest["source_sha"],
                "image_count": 4,
                "bundle_sha256": manifest["bundle"]["sha256"],
                "path": str(folder),
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never include exceptions/URLs/headers, which may carry the temporary SAS.
        print(
            "Deployment artifact download or verification failed; private staging retained.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
