"""Bootstrap for restricted execution of generated programs.

Invoked as ``python -I code_exec_bootstrap.py <target.py>``.  It applies the
resource caps *inside* the child (so the parent never needs ``preexec_fn``,
which is unsafe in a threaded process) and then runs the target as
``__main__`` so tracebacks still point at the generated file.

Standard output/standard error are already redirected to files by the caller,
which keeps the child free of pipes and bounds memory regardless of how much
the program prints.
"""

from __future__ import annotations

import os
import runpy
import sys


def _apply_limits() -> None:
    try:
        import resource  # POSIX only
    except ImportError:
        return
    try:
        cpu = int(os.environ.get("AGENT_CODE_EXEC_CPU_SECONDS", "20"))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    except Exception:
        pass
    try:
        mem_mb = int(os.environ.get("AGENT_CODE_EXEC_MEMORY_MB", "768"))
        limit = mem_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    except Exception:
        pass


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: code_exec_bootstrap.py <target.py>", file=sys.stderr)
        return 2
    target = argv[1]
    _apply_limits()
    try:
        runpy.run_path(target, run_name="__main__")
    except SystemExit as exc:                      # honour the program's own code
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
