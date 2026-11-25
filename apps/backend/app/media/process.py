"""No-shell media subprocesses with absolute deadlines and bounded output/storage."""

import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from app.media.contracts import MediaError

MAX_PROCESS_STDOUT = 256 * 1024
MAX_PROCESS_STDERR = 64 * 1024
# Virtual address space includes shared codec libraries and allocator arenas;
# it is distinct from resident RAM. The renderer's existing 512 MiB cgroup
# memory ceiling is unchanged; allow room for 1080p codec virtual mappings.
MAX_PROCESS_ADDRESS_SPACE = 768 * 1024**2


def _failure_code(errors):
    """Classify known tool failures without exposing stderr, paths or media text."""
    lowered = errors.lower()
    if any(
        token in lowered
        for token in (
            b"cannot allocate memory",
            b"out of memory",
            b"memory allocation",
            b"malloc",
            b"failed to allocate",
        )
    ):
        return "media_memory_limit"
    if b"no such filter" in lowered:
        return "media_filter_unavailable"
    if b"pthread_create failed" in lowered:
        return "media_process_resource_limit"
    return "media_processing_failed"


def checked_file(path, maximum=512 * 1024**2):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise MediaError("unsafe_media_path")
    try:
        info = path.stat()
    except OSError:
        raise MediaError("media_file_unavailable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= maximum:
        raise MediaError("invalid_media_file")
    return path


def scratch_usage(root):
    root = Path(root).absolute()
    if root.resolve() != root:
        raise MediaError("unsafe_scratch_path")
    total, entries = 0, 0
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in [*folders, *files]:
            entries += 1
            info = (Path(directory) / name).lstat()
            if (
                entries > 10_000
                or stat.S_ISLNK(info.st_mode)
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
            ):
                raise MediaError("unsafe_scratch_entry")
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


class ProcessRunner:
    def __init__(
        self,
        *,
        timeout_seconds=30,
        scratch=None,
        max_scratch_bytes=None,
        output=None,
        max_output_bytes=512 * 1024**2,
        admission=None,
    ):
        self.deadline = time.monotonic() + timeout_seconds
        self.scratch = Path(scratch).absolute() if scratch is not None else None
        self.max_scratch_bytes = max_scratch_bytes
        self.output = Path(output).absolute() if output is not None else None
        self.max_output_bytes = max_output_bytes
        self.admission = admission

    def guard(self):
        if time.monotonic() >= self.deadline:
            raise MediaError("media_processing_timeout", retryable=True)
        if self.admission is not None and self.admission() is not True:
            raise MediaError("host_admission_refused", retryable=True)
        if self.scratch is not None and scratch_usage(self.scratch) > self.max_scratch_bytes:
            raise MediaError("scratch_quota_exceeded")
        if self.output is not None and self.output.exists():
            info = self.output.lstat()
            if not stat.S_ISREG(info.st_mode) or self.output.is_symlink():
                raise MediaError("unsafe_media_path")
            if info.st_size > self.max_output_bytes:
                raise MediaError("render_output_too_large")

    def run(self, arguments, *, cwd=None, progress=None):
        self.guard()
        binary = shutil.which(arguments[0])
        if binary is None:
            raise MediaError("media_tool_unavailable")
        maximum = min(self.max_output_bytes, self.max_scratch_bytes or self.max_output_bytes)
        command = [
            sys.executable,
            "-m",
            "app.media.child",
            str(os.getpid()),
            str(MAX_PROCESS_ADDRESS_SPACE),
            str(maximum),
            binary,
            *map(str, arguments[1:]),
        ]
        # The decoder inherits no API, database, broker, or object-store secrets.
        environment = {
            "PATH": os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MALLOC_ARENA_MAX": "2",
        }
        process = None
        output, errors, pending = bytearray(), bytearray(), bytearray()
        last_guard = 0.0
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                env=environment,
            )
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    now = time.monotonic()
                    if now - last_guard >= 0.25:
                        self.guard()
                        last_guard = now
                    for key, _ in selector.select(timeout=0.1):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        if key.fileobj is process.stderr:
                            errors.extend(chunk)
                            if len(errors) > MAX_PROCESS_STDERR:
                                raise MediaError("media_process_output_limit")
                        elif progress is not None:
                            pending.extend(chunk)
                            if len(pending) > 4096:
                                raise MediaError("media_process_output_limit")
                            while b"\n" in pending:
                                line, _, rest = pending.partition(b"\n")
                                pending[:] = rest
                                if line.startswith(b"out_time_us="):
                                    try:
                                        progress(max(0, int(line.partition(b"=")[2])) / 1_000_000)
                                    except ValueError:
                                        pass
                        else:
                            output.extend(chunk)
                            if len(output) > MAX_PROCESS_STDOUT:
                                raise MediaError("media_process_output_limit")
                remaining = self.deadline - time.monotonic()
                process.wait(timeout=max(0.01, remaining))
            self.guard()
            if process.returncode != 0:
                raise MediaError(_failure_code(errors))
            return bytes(output)
        except MediaError:
            raise
        except (OSError, subprocess.SubprocessError):
            raise MediaError("media_processing_failed") from None
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=0.5)
                    except (OSError, subprocess.TimeoutExpired):
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except OSError:
                            pass
                        process.wait(timeout=2)
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
