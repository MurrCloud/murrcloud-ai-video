from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_stack_isolation_and_bounded_resources():
    stack = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert stack["name"] == "murrcloud-video-studio"
    assert stack["networks"]["video_internal"]["internal"] is True
    services = stack["services"]
    assert set(services) == {
        "api",
        "postgres",
        "redis",
        "generation-worker",
        "narration-worker",
        "rendering-worker",
        "frontend",
    }
    assert sum(float(s["cpus"]) for s in services.values()) <= 1.5
    total_mb = sum(int(str(s["mem_limit"]).removesuffix("m")) for s in services.values())
    assert total_mb <= 2560
    for name, service in services.items():
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert int(service["user"].split(":")[0]) != 0
        assert 0 < service["pids_limit"] <= 128
        assert service.get("privileged") is not True
        for mount in service.get("volumes", []):
            assert "docker.sock" not in str(mount)
            # Only explicitly declared Studio volumes or repository helper mounts
            # are allowed; no unrelated workload storage is present.
            source = (
                mount.get("source", "") if isinstance(mount, dict) else str(mount).split(":")[0]
            )
            assert source in {
                "video_storage",
                "video_postgres",
                "video_redis",
            } or source.startswith("./infrastructure/")
        if name == "frontend":
            assert service["ports"] == ["127.0.0.1:3100:3100"]
            assert service["profiles"] == ["ui"]
        elif name != "api":
            assert not service.get("ports")
        else:
            assert service["ports"] == ["127.0.0.1:8100:8100"]


def test_production_cannot_enable_paid_jobs_or_rendering():
    prod = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
    for service in prod["services"].values():
        env = service["environment"]
        assert env["VIDEO_ENVIRONMENT"] == "production"
        assert env["VIDEO_COOKIE_SECURE"] == "true"
        assert env["VIDEO_RENDER_ENABLED"] == "false"
        assert env["VIDEO_PAID_GENERATION_ENABLED"] == "false"
        signal = service["volumes"][0]
        assert signal["read_only"] is True
        assert signal["bind"]["create_host_path"] is False


def test_archive_credentials_are_scoped_to_render_worker_and_default_gate_is_off():
    stack = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for name, service in stack["services"].items():
        environment = service.get("environment", {})
        if name in {"api", "generation-worker", "narration-worker", "rendering-worker"}:
            assert environment.get("VIDEO_S3_ENABLED") == "false"
        if name != "rendering-worker":
            assert "VIDEO_S3_SECRET_ACCESS_KEY" not in environment
            assert "VIDEO_S3_ACCESS_KEY_ID" not in environment
