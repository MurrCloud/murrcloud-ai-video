"""Exec a backup tool with hard limits in a single-threaded child."""

import ctypes
import os
import resource
import signal
import sys


def main():
    parent, maximum_file, maximum_cpu = map(int, sys.argv[1:4])
    arguments = sys.argv[4:]
    if parent <= 1 or maximum_file <= 0 or maximum_cpu <= 0 or not arguments:
        raise SystemExit(2)
    os.umask(0o077)
    if sys.platform.startswith("linux"):
        library = ctypes.CDLL(None, use_errno=True)
        if library.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
            raise SystemExit(2)
        # Go's age runtime reserves a large virtual arena; this is a virtual
        # ceiling, independent of the reviewed container's resident RAM limit.
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (maximum_file, maximum_file))
    resource.setrlimit(resource.RLIMIT_CPU, (maximum_cpu, maximum_cpu))
    try:
        os.nice(10)
    except OSError:
        pass
    os.execv(arguments[0], arguments)


if __name__ == "__main__":
    main()
