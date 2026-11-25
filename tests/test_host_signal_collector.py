"""Probe configuration stays explicit and refuses credentials and redirects."""

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

COLLECTOR = Path(__file__).resolve().parents[1] / "infrastructure/monitoring/collect-host-signal.py"
SPEC = importlib.util.spec_from_file_location("host_signal_collector", COLLECTOR)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


@pytest.mark.parametrize(
    "value",
    [
        "http://erp.example.test/health",
        "https://user:password@erp.example.test/health",
        "https://erp.example.test/health?token=synthetic",
        "https://erp.example.test/health#private",
        "https://erp.example.test:8443/health",
        "https://erp.example.test:invalid/health",
        "https://erp.example.test/health\n",
        "",
    ],
)
def test_probe_rejects_unsafe_target(value):
    with pytest.raises(argparse.ArgumentTypeError):
        collector.health_target(value)


def test_reviewed_https_probe_is_accepted_and_redirect_is_refused():
    target = "https://erp.example.test/health"
    assert collector.health_target(target) == target
    assert collector.NoRedirect().redirect_request(None, None, None, None, target) is None


def test_probe_requires_target_and_all_protected_units_before_host_access(tmp_path):
    required = {
        "--erp-unit": "erp.service",
        "--postgres-unit": "postgresql.service",
        "--nginx-unit": "nginx.service",
        "--erp-health-url": "https://erp.example.test/health",
    }
    for missing in required:
        args = [sys.executable, str(COLLECTOR), "--output", str(tmp_path / "host.json")]
        for flag, value in required.items():
            if flag != missing:
                args.extend([flag, value])
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        assert result.returncode == 2
        assert missing in result.stderr
        assert not (tmp_path / "host.json").exists()
