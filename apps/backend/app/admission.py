"""Conservative host guard. Missing, stale or malformed evidence refuses jobs."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import os
import stat

MAX_AGE_SECONDS = 90
MIN_AVAILABLE_MEMORY_BYTES = 2 * 1024**3
MIN_ROOT_FREE_BYTES = 15 * 1024**3


def evaluate(path: str, *, now: dt.datetime | None = None) -> tuple[bool, str]:
    try:
        with open(path, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                return False, "invalid_signal"
            fcntl.flock(source.fileno(), fcntl.LOCK_SH)
            raw = source.read(16385)
            if len(raw) > 16384:
                return False, "invalid_signal"
            signal = json.loads(raw)
        if (
            not isinstance(signal, dict)
            or type(signal.get("schema_version")) is not int
            or signal["schema_version"] != 1
        ):
            return False, "invalid_signal"
        observed = dt.datetime.fromisoformat(signal["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return False, "invalid_signal"
        age = ((now or dt.datetime.now(dt.UTC)) - observed).total_seconds()
        if age < -5 or age > MAX_AGE_SECONDS:
            return False, "stale_signal"
        for key in ("memory_available_bytes", "root_free_bytes", "cpu_count"):
            if type(signal.get(key)) is not int or signal[key] <= 0:
                return False, "invalid_signal"
        load = signal.get("load1")
        if type(load) not in {int, float} or not math.isfinite(load) or load < 0:
            return False, "invalid_signal"
        services = signal.get("services")
        if not isinstance(services, dict) or any(
            services.get(name) is not True for name in ("erp", "postgresql", "nginx")
        ):
            return False, "protected_service_unhealthy"
        if signal["memory_available_bytes"] < MIN_AVAILABLE_MEMORY_BYTES:
            return False, "memory_pressure"
        if signal["root_free_bytes"] < MIN_ROOT_FREE_BYTES:
            return False, "disk_pressure"
        if load > signal["cpu_count"] * 0.75:
            return False, "cpu_pressure"
        latency = signal.get("erp_response_ms")
        if (
            signal.get("erp_http_status") != 200
            or type(latency) not in {int, float}
            or not math.isfinite(latency)
            or latency < 0
            or latency > 1000
        ):
            return False, "erp_http_unhealthy"
        return True, "admitted"
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return False, "invalid_signal"
