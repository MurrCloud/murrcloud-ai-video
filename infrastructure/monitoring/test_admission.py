"""Behavioral checks for resource admission failures and boundary conditions."""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from admission import evaluate


class HostAdmissionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "host.json"
        self.now = dt.datetime(2026, 11, 6, tzinfo=dt.UTC)
        self.signal = {
            "schema_version": 1,
            "observed_at": self.now.isoformat(),
            "memory_available_bytes": 3 * 1024**3,
            "root_free_bytes": 20 * 1024**3,
            "cpu_count": 2,
            "load1": 0.25,
            "erp_http_status": 200,
            "erp_response_ms": 75.0,
            "services": {"erp": True, "postgresql": True, "nginx": True},
        }

    def check(self):
        self.path.write_text(json.dumps(self.signal))
        return evaluate(str(self.path), now=self.now)

    def test_fresh_healthy_host_is_admitted(self):
        self.assertEqual(self.check(), (True, "admitted"))

    def test_missing_and_broken_signal_refuse(self):
        self.assertEqual(evaluate(str(self.path), now=self.now), (False, "invalid_signal"))
        self.path.write_text("partial{")
        self.assertEqual(evaluate(str(self.path), now=self.now), (False, "invalid_signal"))

    def test_stale_future_and_naive_times_refuse(self):
        for difference in (-91, 6):
            with self.subTest(difference=difference):
                self.signal["observed_at"] = (
                    self.now + dt.timedelta(seconds=difference)
                ).isoformat()
                self.assertEqual(self.check(), (False, "stale_signal"))
        self.signal["observed_at"] = "2026-11-06T00:00:00"
        self.assertEqual(self.check(), (False, "invalid_signal"))

    def test_any_protected_service_failure_refuses(self):
        for service in self.signal["services"]:
            with self.subTest(service=service):
                self.signal["services"][service] = False
                self.assertEqual(self.check(), (False, "protected_service_unhealthy"))
                self.signal["services"][service] = True

    def test_pressure_gates_are_independent(self):
        for field, value, reason in (
            ("memory_available_bytes", 2 * 1024**3 - 1, "memory_pressure"),
            ("root_free_bytes", 15 * 1024**3 - 1, "disk_pressure"),
            ("load1", 1.5001, "cpu_pressure"),
        ):
            with self.subTest(field=field):
                previous = self.signal[field]
                self.signal[field] = value
                self.assertEqual(self.check(), (False, reason))
                self.signal[field] = previous

    def test_booleans_and_nonfinite_numbers_are_not_capacity(self):
        for field, value in (
            ("cpu_count", True),
            ("memory_available_bytes", True),
            ("load1", float("nan")),
            ("load1", float("inf")),
        ):
            with self.subTest(field=field, value=value):
                previous = self.signal[field]
                self.signal[field] = value
                self.assertEqual(self.check(), (False, "invalid_signal"))
                self.signal[field] = previous

    def test_erp_probe_failure_or_latency_refuses(self):
        for field, value in (
            ("erp_http_status", 503),
            ("erp_response_ms", None),
            ("erp_response_ms", float("nan")),
            ("erp_response_ms", 1000.01),
        ):
            with self.subTest(field=field, value=value):
                previous = self.signal[field]
                self.signal[field] = value
                self.assertEqual(self.check(), (False, "erp_http_unhealthy"))
                self.signal[field] = previous


if __name__ == "__main__":
    unittest.main()
