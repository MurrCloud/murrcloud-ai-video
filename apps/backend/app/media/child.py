"""Exec a bounded media tool; Linux kills it if its Celery parent disappears."""

import ctypes
import os
import resource
import signal
import sys


def main():
    parent, memory, maximum_file = map(int, sys.argv[1:4])
    arguments = sys.argv[4:]
    if not arguments or parent <= 1 or memory <= 0 or maximum_file <= 0:
        raise SystemExit(2)
    if sys.platform.startswith("linux"):
        # Set in this single-threaded child, never via unsafe threaded preexec_fn.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise SystemExit(2)
        if os.getppid() != parent:
            raise SystemExit(2)
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (maximum_file, maximum_file))
    try:
        os.nice(10)
    except OSError:
        # Sandboxed developer runtimes may forbid even lowering priority. The
        # deployed CPU quota and one-thread configuration remain authoritative.
        pass
    os.execv(arguments[0], arguments)


if __name__ == "__main__":
    main()
