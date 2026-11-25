"""Bound S3 SDK wall time in a secret-isolated child, never in an API request."""

import ctypes
import json
import os
import resource
import signal
import sys
from types import SimpleNamespace


def main():
    raw = sys.stdin.buffer.read(16385)
    if len(raw) > 16384:
        raise ValueError
    request = json.loads(raw)
    if type(request.get("parent_pid")) is not int or request["parent_pid"] <= 1:
        raise ValueError
    if sys.platform.startswith("linux"):
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise ValueError
        if os.getppid() != request["parent_pid"]:
            raise ValueError
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    import boto3
    from botocore.config import Config

    from app.production_storage import S3Archive

    client = boto3.client(
        "s3",
        endpoint_url=request["endpoint"],
        aws_access_key_id=request["access_key"],
        aws_secret_access_key=request["secret_key"],
        region_name=request["region"],
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=30,
            retries={"mode": "standard", "total_max_attempts": 2},
            s3={"addressing_style": "path"},
            proxies={},
        ),
    )
    settings = SimpleNamespace(
        s3_enabled=True,
        storage_dir=request["storage_dir"],
        max_media_bytes=request["max_media_bytes"],
        s3_bucket=request["bucket"],
    )
    try:
        reply = S3Archive(settings, client).archive(
            request["path"],
            request["key"],
            request["mime_type"],
            request["sha256"],
            request["size_bytes"],
        )
        sys.stdout.write(json.dumps(reply))
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.stdout.write('{"status":"failed"}')
        raise SystemExit(2) from None
