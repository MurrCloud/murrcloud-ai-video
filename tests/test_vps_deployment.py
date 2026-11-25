"""Safety-critical VPS configuration invariants, without Docker or remote actions."""

import configparser
import ipaddress
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
VPS = ROOT / "infrastructure/docker/compose.vps.yml"
UNIT = ROOT / "infrastructure/systemd/murrcloud-video-studio.service.example"
NGINX = ROOT / "infrastructure/nginx/video.example.test.vps.conf.example"
BODY_MOUNT = ROOT / "infrastructure/systemd/studio-nginx-body.mount.example"
SERVICES = {"api", "frontend", "postgres", "redis"}
GATES = {
    "VIDEO_PAID_GENERATION_ENABLED",
    "VIDEO_DIRECTOR_ENABLED",
    "VIDEO_NARRATION_ENABLED",
    "VIDEO_RENDER_ENABLED",
    "VIDEO_S3_ENABLED",
}


class EmptyReset(list):
    """Keep Compose's explicit empty reset distinct from an ordinary sequence."""


class ComposeLoader(yaml.SafeLoader):
    pass


def empty_reset(loader, node):
    if not isinstance(node, yaml.SequenceNode) or node.value:
        raise yaml.constructor.ConstructorError(
            None, None, "Only an empty sequence reset is accepted", node.start_mark
        )
    return EmptyReset()


ComposeLoader.add_constructor("!reset", empty_reset)


def vps_config():
    return yaml.load(VPS.read_text(), Loader=ComposeLoader)


def directives(key):
    return [
        line.split("=", 1)[1]
        for line in UNIT.read_text().splitlines()
        if line.startswith(key + "=")
    ]


def test_vps_uses_only_imported_images_and_dedicated_bounded_paths():
    config = vps_config()
    assert set(config["services"]) == SERVICES
    for name, service in config["services"].items():
        assert service["image"].startswith("${VIDEO_") and ":?" in service["image"]
        assert service["pull_policy"] == "never" and service["platform"] == "linux/amd64"
        assert service["restart"] == "no", f"{name} must not bypass the mount guard at daemon boot"
        assert not set(service) & {"privileged", "network_mode", "devices", "cap_add"}
        if name in {"api", "frontend"}:
            assert type(service["ports"]) is EmptyReset and not service["ports"]
        else:
            assert "ports" not in service
    for volume, suffix in (
        ("video_postgres", "postgres"),
        ("video_redis", "redis"),
        ("video_storage", "media"),
    ):
        assert config["volumes"][volume] == {
            "driver": "local",
            "driver_opts": {
                "type": "none",
                "o": "bind",
                "device": "/srv/video-studio/" + suffix,
            },
        }
    environment = config["services"]["api"]["environment"]
    assert all(environment[gate] == "false" for gate in GATES)
    assert environment["VIDEO_FAL_KEY"] == environment["VIDEO_OPENAI_API_KEY"] == ""
    command = config["services"]["api"]["command"]
    assert command[command.index("--workers") + 1] == "1"
    assert (
        command[command.index("--forwarded-allow-ips") + 1]
        == "${VIDEO_TRUSTED_PROXY_IPS:-127.0.0.1}"
    )


@pytest.mark.parametrize("value", ["!reset null", "!reset {}", "!reset [8100]", "!unsafe []"])
def test_compose_loader_rejects_nonempty_or_unknown_reset_forms(value):
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.load("ports: " + value, Loader=ComposeLoader)


def test_private_upstreams_remain_reserved_and_match_nginx_without_host_ports():
    config = vps_config()
    network = config["networks"]["video_internal"]
    assert network["internal"] is True
    ipam = network["ipam"]["config"]
    assert len(ipam) == 1
    subnet = ipaddress.ip_network(ipam[0]["subnet"])
    dynamic = ipaddress.ip_network(ipam[0]["ip_range"])
    gateway = ipaddress.ip_address(ipam[0]["gateway"])
    assert str(subnet) == "172.28.100.0/28" and subnet.is_private
    assert str(dynamic) == "172.28.100.8/29" and dynamic.subnet_of(subnet)
    assert str(gateway) == "172.28.100.1" and gateway in subnet
    assert not subnet.overlaps(ipaddress.ip_network("172.17.0.0/16"))  # Default bridge.
    expected = {"api": ("172.28.100.2", 8100), "frontend": ("172.28.100.3", 3100)}
    assigned = {gateway}
    for name, (address, _) in expected.items():
        service = config["services"][name]
        assert service["networks"] == {"video_internal": {"ipv4_address": address}}
        ip = ipaddress.ip_address(address)
        assert ip in subnet and ip not in dynamic and ip not in assigned
        assert ip not in {subnet.network_address, subnet.broadcast_address}
        assigned.add(ip)
    proxy = NGINX.read_text()
    assert set(re.findall(r"proxy_pass http://([^;]+);", proxy)) == {
        f"{address}:{port}" for address, port in expected.values()
    }
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in proxy
    assert "$proxy_add_x_forwarded_for" not in proxy
    assert "ssl_certificate /etc/letsencrypt/live/video.example.test/fullchain.pem;" in proxy


def test_shared_nginx_body_staging_survives_studio_data_filesystem_failure():
    proxy = NGINX.read_text()
    body_path = "/var/cache/video-studio/nginx-body"
    assert re.findall(r"client_body_temp_path\s+([^;]+);", proxy) == [body_path + " 1 2"]
    # Streaming HTTP/1.1 also avoids forced buffering for chunked client bodies.
    for name in ("/api/", "/"):
        locations = [
            value
            for value in re.findall(r"location " + re.escape(name) + r" \{([^}]+)\}", proxy)
            if "proxy_pass " in value
        ]
        assert len(locations) == 1
        assert "proxy_request_buffering off;" in locations[0]
        assert "proxy_http_version 1.1;" in locations[0]
    unit = configparser.ConfigParser(interpolation=None)
    unit.optionxform = str
    unit.read_string(BODY_MOUNT.read_text())
    assert set(unit.sections()) == {"Unit", "Mount", "Install"}
    assert set(unit["Unit"]) == {"Description"}
    assert dict(unit["Mount"]) == {
        "What": "tmpfs",
        "Where": body_path,
        "Type": "tmpfs",
        "Options": "size=16M,mode=0700,uid=33,gid=33,nodev,nosuid,noexec",
        "TimeoutSec": "15",
    }
    assert dict(unit["Install"]) == {"WantedBy": "multi-user.target"}
    assert not any(
        "nginx.service" in value or "/srv/" in value
        for section in unit.values()
        for value in section.values()
    )


def test_service_fails_closed_before_start_and_stops_only_studio():
    assert directives("RequiresMountsFor") == ["/srv/video-studio"]
    assert directives("WorkingDirectory") == ["/opt/video-studio/current"]
    assert directives("EnvironmentFile") == ["/opt/video-studio/shared/studio.env"]
    assert directives("Type") == ["oneshot"] and directives("RemainAfterExit") == ["yes"]
    assert directives("Restart") == ["no"]
    checks = directives("ExecStartPre")
    assert checks[0] == "/usr/bin/mountpoint -q /srv/video-studio"
    assert checks[1] == "/usr/bin/test -f /srv/video-studio/.studio-filesystem"
    assert checks[-1].endswith("config --quiet")
    start = shlex.split(directives("ExecStart")[0])
    assert start[:2] == ["/usr/bin/docker", "compose"]
    assert start[start.index("--project-name") + 1] == "murrcloud-video-studio"
    assert start[start.index("--profile") + 1] == "ui"
    assert "--no-build" in start and start[start.index("--pull") + 1] == "never"
    assert "--wait" in start and set(start[-4:]) == SERVICES
    assert [start[index + 1] for index, value in enumerate(start) if value == "-f"] == [
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "infrastructure/docker/compose.vps.yml",
    ]
    for key in ("ExecStop", "ExecStopPost"):
        stop = shlex.split(directives(key)[0].lstrip("-"))
        assert stop[stop.index("--project-name") + 1] == "murrcloud-video-studio"
        assert "stop" in stop and set(stop[-4:]) == SERVICES
        assert not set(stop) & {"down", "--volumes", "restart", "prune"}


@pytest.mark.parametrize(
    "value,valid",
    [
        (None, True),
        ("127.0.0.1,172.30.0.1", True),
        ("::1,172.30.0.1", True),
        ("*", False),
        ("0.0.0.0/0", False),
        ("", False),
        ("127.0.0.1,*", False),
        ("not-a-proxy", False),
        ("127.0.0.1," * 5, False),
    ],
)
def test_startup_proxy_guard_accepts_only_bounded_exact_addresses(value, valid):
    # Execute the actual service preflight, rather than duplicating its validator.
    argv = shlex.split(next(line for line in directives("ExecStartPre") if "ipaddress" in line))
    environment = dict(os.environ)
    environment.pop("VIDEO_TRUSTED_PROXY_IPS", None)
    if value is not None:
        environment["VIDEO_TRUSTED_PROXY_IPS"] = value
    result = subprocess.run(
        [sys.executable, *argv[1:]], env=environment, capture_output=True, timeout=5
    )
    assert (result.returncode == 0) is valid
