"""The storage operation refuses unsafe host inputs before any mutation."""

import importlib.util
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

GIB = 1024**3
EXPECTED_MACHINE = "1234567890abcdef1234567890abcdef"
PROTECTED_UNITS = (
    "example-app.service",
    "example-database.service",
    "nginx.service",
    "docker.service",
)


@pytest.fixture
def storage_preflight(tmp_path, monkeypatch):
    filename = Path(__file__).resolve().parents[1] / "scripts/prepare-vps-storage.py"
    spec = importlib.util.spec_from_file_location("studio_storage_preflight_test", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path.resolve()
    for name, basename in (("ROOT", "data"), ("MOUNT", "mount"), ("APP", "application")):
        monkeypatch.setattr(module, name, root / basename)
    values = SimpleNamespace(
        uid=0,
        machine=EXPECTED_MACHINE,
        available_kib=5 * 1024**2,
        free_bytes=40 * GIB,
        services={unit: "active" for unit in PROTECTED_UNITS},
        service_error=None,
    )
    commands = []

    def path(value):
        if str(value) == "/etc/machine-id":
            return SimpleNamespace(read_text=lambda: values.machine + "\n")
        if str(value) == "/proc/meminfo":
            return SimpleNamespace(
                read_text=lambda: (
                    "MemTotal: 8000000 kB\n"
                    f"MemAvailable: {values.available_kib} kB\n"
                    "SwapTotal: 0 kB\n"
                )
            )
        raise AssertionError("Preflight attempted to read an unexpected host path")

    def checked_output(arguments, *, text, timeout):
        arguments = tuple(arguments)
        commands.append(arguments)
        assert text is True and 0 < timeout <= 30
        assert arguments[:2] == ("systemctl", "is-active") and len(arguments) == 3, (
            "No formatting, mount, service mutation or other subprocess may run in preflight"
        )
        assert arguments[2] in PROTECTED_UNITS
        if values.service_error is not None:
            raise values.service_error
        return values.services[arguments[2]] + "\n"

    original_statvfs = module.os.statvfs

    def statvfs(value):
        if str(value) == "/":
            return SimpleNamespace(f_bavail=values.free_bytes, f_frsize=1)
        return original_statvfs(value)

    monkeypatch.setattr(module, "Path", path)
    monkeypatch.setattr(module.os, "geteuid", lambda: values.uid)
    monkeypatch.setattr(module.os, "statvfs", statvfs)
    monkeypatch.setattr(module.subprocess, "check_output", checked_output)

    def snapshot():
        entries = []
        for item in sorted(root.rglob("*")):
            mode = item.lstat().st_mode
            contents = (
                str(item.readlink())
                if stat.S_ISLNK(mode)
                else item.read_bytes()
                if stat.S_ISREG(mode)
                else None
            )
            entries.append((str(item.relative_to(root)), mode, contents))
        return entries

    return SimpleNamespace(
        module=module, values=values, commands=commands, root=root, snapshot=snapshot
    )


def assert_refused(fixture, reason):
    before = fixture.snapshot()
    with pytest.raises(SystemExit, match=reason):
        fixture.module.validate_host(EXPECTED_MACHINE, PROTECTED_UNITS)
    assert fixture.snapshot() == before, "Rejected preflight changed Studio files or directories"


def test_healthy_host_passes_without_creating_storage_or_mutating_services(storage_preflight):
    fixture = storage_preflight
    assert fixture.module.validate_host(EXPECTED_MACHINE, PROTECTED_UNITS) is None
    assert fixture.snapshot() == []
    assert fixture.commands == [("systemctl", "is-active", unit) for unit in PROTECTED_UNITS]


@pytest.mark.parametrize("field,value", [("uid", 1000), ("machine", "another-server-machine-id")])
def test_wrong_host_or_nonroot_identity_refuses_before_other_probes(
    storage_preflight, field, value
):
    fixture = storage_preflight
    setattr(fixture.values, field, value)
    assert_refused(fixture, "reviewed root/host identity required")
    assert fixture.commands == []


def test_insufficient_host_memory_refuses_before_service_probes(storage_preflight):
    fixture = storage_preflight
    fixture.values.available_kib = 3 * 1024**2 - 1
    assert_refused(fixture, "insufficient available host memory")
    assert fixture.commands == []


def test_disk_reserve_covers_backing_file_retained_space_and_staging(storage_preflight):
    fixture = storage_preflight
    fixture.values.free_bytes = 27 * GIB - 1
    assert_refused(fixture, "insufficient root free-space reserve")
    assert fixture.commands == []


def test_exact_minimum_capacity_passes_without_changing_files(storage_preflight):
    fixture = storage_preflight
    fixture.values.available_kib = 3 * 1024**2
    fixture.values.free_bytes = 27 * GIB
    assert fixture.module.validate_host(EXPECTED_MACHINE, PROTECTED_UNITS) is None
    assert fixture.snapshot() == []


@pytest.mark.parametrize("unit", PROTECTED_UNITS)
def test_any_unavailable_protected_service_refuses_before_storage_creation(storage_preflight, unit):
    fixture = storage_preflight
    fixture.values.services[unit] = "inactive"
    assert_refused(fixture, "protected service unavailable")


def test_failed_service_probe_cannot_continue_to_storage_mutation(storage_preflight):
    fixture = storage_preflight
    fixture.values.service_error = subprocess.CalledProcessError(
        3, ["systemctl", "is-active", "example-app.service"]
    )
    before = fixture.snapshot()
    with pytest.raises(subprocess.CalledProcessError):
        fixture.module.validate_host(EXPECTED_MACHINE, PROTECTED_UNITS)
    assert fixture.snapshot() == before
    assert len(fixture.commands) == 1


@pytest.mark.parametrize("name", ["ROOT", "MOUNT", "APP"])
@pytest.mark.parametrize("kind", ["directory", "file", "dangling_symlink"])
def test_existing_or_linked_studio_target_is_never_reused(storage_preflight, name, kind):
    fixture = storage_preflight
    path = getattr(fixture.module, name)
    if kind == "directory":
        path.mkdir()
        (path / "existing-sentinel").write_text("Do not change existing data")
    elif kind == "file":
        path.write_text("Do not overwrite an existing file")
    else:
        path.symlink_to(fixture.root / "absent-symlink-target")
    assert_refused(fixture, "Studio path already exists or parent is linked")


@pytest.mark.parametrize("name", ["ROOT", "MOUNT", "APP"])
def test_linked_parent_cannot_redirect_new_storage_elsewhere(storage_preflight, monkeypatch, name):
    fixture = storage_preflight
    destination = fixture.root / "unrelated-directory"
    destination.mkdir()
    (destination / "sentinel").write_text("Preserve unrelated files")
    linked = fixture.root / "linked-parent"
    linked.symlink_to(destination, target_is_directory=True)
    monkeypatch.setattr(fixture.module, name, linked / "new-studio-path")
    assert_refused(fixture, "Studio path already exists or parent is linked")
    assert not (destination / "new-studio-path").exists()


@pytest.mark.parametrize("expected", [None, "", "not-a-machine-id", "0" * 31])
def test_missing_or_invalid_expected_host_refuses_before_any_host_probe(
    storage_preflight, expected
):
    fixture = storage_preflight
    with pytest.raises(SystemExit, match="explicit expected machine identity required"):
        fixture.module.validate_host(expected, PROTECTED_UNITS)
    assert fixture.snapshot() == []
    assert fixture.commands == []


@pytest.mark.parametrize("services", [(), ("",), ("--unsafe",), ("app.service; restart",)])
def test_missing_or_invalid_protected_inventory_refuses(storage_preflight, services):
    fixture = storage_preflight
    with pytest.raises(SystemExit, match="explicit protected service inventory required"):
        fixture.module.validate_host(EXPECTED_MACHINE, services)
    assert fixture.snapshot() == []
    assert fixture.commands == []


@pytest.fixture
def artifact_tool(tmp_path, monkeypatch):
    filename = Path(__file__).resolve().parents[1] / "scripts/fetch-deployment-artifact.py"
    spec = importlib.util.spec_from_file_location("studio_artifact_verification_test", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path.resolve()
    monkeypatch.setattr(module, "ROOT", root)
    values = SimpleNamespace(uid=0, machine=EXPECTED_MACHINE)
    reads = []

    def path(value):
        assert str(value) == "/etc/machine-id"
        reads.append(str(value))
        return SimpleNamespace(read_text=lambda: values.machine + "\n")

    monkeypatch.setattr(module, "Path", path)
    monkeypatch.setattr(module.os, "geteuid", lambda: values.uid)
    return SimpleNamespace(module=module, values=values, reads=reads, root=root)


@pytest.mark.parametrize("expected", [None, "", "invalid", "0" * 31])
def test_artifact_tool_requires_explicit_valid_host_before_reading_identity(
    artifact_tool, expected
):
    with pytest.raises(ValueError, match="artifact_verification_failed"):
        artifact_tool.module.validate_host(expected)
    assert artifact_tool.reads == []
    assert list(artifact_tool.root.iterdir()) == []


@pytest.mark.parametrize("field,value", [("uid", 1000), ("machine", "f" * 32)])
def test_artifact_tool_refuses_wrong_host_or_nonroot_before_download(artifact_tool, field, value):
    setattr(artifact_tool.values, field, value)
    with pytest.raises(ValueError, match="artifact_verification_failed"):
        artifact_tool.module.validate_host(EXPECTED_MACHINE)
    assert list(artifact_tool.root.iterdir()) == []


def test_artifact_tool_accepts_matching_reviewed_host_without_mutation(artifact_tool):
    assert artifact_tool.module.validate_host(EXPECTED_MACHINE) is None
    assert artifact_tool.reads == ["/etc/machine-id"]
    assert list(artifact_tool.root.iterdir()) == []


@pytest.mark.parametrize(
    "repository", [None, "", "repo-only", "../repo", "owner/../repo", "owner/repo;bad"]
)
def test_artifact_tool_requires_explicit_repository(artifact_tool, repository):
    with pytest.raises(ValueError, match="artifact_verification_failed"):
        artifact_tool.module.validate_repository(repository)


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "other/example-studio"),
        ("source_sha", "b" * 40),
        ("workflow_run_id", "43"),
        ("platform", "linux/arm64"),
    ],
)
def test_artifact_manifest_origin_must_match_reviewed_expectations(artifact_tool, field, value):
    manifest = {
        "repository": "example/example-studio",
        "source_sha": "a" * 40,
        "workflow_run_id": "42",
        "platform": "linux/amd64",
    }
    payload = {"source_sha": "a" * 40, "run_id": 42}
    assert (
        artifact_tool.module.validate_manifest_origin(manifest, payload, "example/example-studio")
        is None
    )
    manifest[field] = value
    with pytest.raises(ValueError, match="artifact_verification_failed"):
        artifact_tool.module.validate_manifest_origin(manifest, payload, "example/example-studio")
