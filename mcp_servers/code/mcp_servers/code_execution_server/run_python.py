"""Python-only launcher: block process creation in the kernel, cap memory, then run stdin code.

Run as ``<profile venv>/bin/python -I run_python.py [--memory-mb N]``, with the code on stdin.

The hard "no shell / no subprocess" boundary is a seccomp filter that makes the kernel refuse
``execve``/``execveat`` for this process and every child, however the call is made — a libc
``system``, ``_posixsubprocess.fork_exec`` or a raw syscall all fail with EACCES. LD_PRELOAD
interposition cannot promise that, because a libc-internal call or a direct syscall never passes
through the interposed symbol. The Python audit hook on top only turns the common attempts into
clear errors. Stdlib only: it runs inside the pinned profile venv.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import linecache
import os
import platform
import resource
import struct
import sys
import traceback

CODE_FILENAME = "<code>"
THREAD_ENV = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)

BLOCKED_EVENT_PREFIXES = (
    "subprocess.",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.spawn",
    "os.fork",
    "os.forkpty",
    "os.kill",
    "os.killpg",
    "socket.",
    "resource.setrlimit",
    "resource.prlimit",
    "webbrowser.",
)
BLOCKED_IMPORTS = frozenset({"pip", "ensurepip"})

# seccomp / prctl constants (linux/seccomp.h, linux/audit.h, sys/prctl.h).
PR_SET_NO_NEW_PRIVS = 38
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
EPERM = 1
# Per-arch (AUDIT_ARCH, execve nr, execveat nr).
_ARCHES = {
    "x86_64": (0xC000003E, 59, 322),
    "aarch64": (0xC00000B7, 221, 281),
}
# classic BPF opcodes
_LD_W_ABS = 0x20
_JEQ = 0x15
_RET = 0x06
_ARCH_OFFSET = 4  # offsetof(struct seccomp_data, arch)
_NR_OFFSET = 0  # offsetof(struct seccomp_data, nr)


class SandboxViolation(PermissionError):
    pass


def _bpf(code: int, k: int, jt: int = 0, jf: int = 0) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def _seccomp_program() -> bytes:
    arch = platform.machine()
    if arch not in _ARCHES:
        raise SandboxViolation(f"sandbox: unsupported architecture {arch!r}")
    audit_arch, execve_nr, execveat_nr = _ARCHES[arch]
    # Fail closed on a foreign arch (a compat/x32 call), then deny the two exec syscalls.
    return b"".join(
        [
            _bpf(_LD_W_ABS, _ARCH_OFFSET),
            _bpf(_JEQ, audit_arch, 0, 4),
            _bpf(_LD_W_ABS, _NR_OFFSET),
            _bpf(_JEQ, execve_nr, 2, 0),
            _bpf(_JEQ, execveat_nr, 1, 0),
            _bpf(_RET, SECCOMP_RET_ALLOW),
            _bpf(_RET, SECCOMP_RET_ERRNO | EPERM),
        ]
    )


def _install_seccomp() -> None:
    """Refuse execve/execveat in the kernel for this process tree. Fail closed."""
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    program = _seccomp_program()
    buf = ctypes.create_string_buffer(program, len(program))

    class _Prog(ctypes.Structure):
        _fields_ = (("len", ctypes.c_ushort), ("filter", ctypes.c_void_p))

    fprog = _Prog(len(program) // 8, ctypes.cast(buf, ctypes.c_void_p))
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise SandboxViolation(
            f"sandbox: PR_SET_NO_NEW_PRIVS failed (errno {ctypes.get_errno()})"
        )
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        raise SandboxViolation(
            f"sandbox: PR_SET_SECCOMP failed (errno {ctypes.get_errno()})"
        )


def _audit_hook(event: str, args: tuple[object, ...]) -> None:
    if event.startswith(BLOCKED_EVENT_PREFIXES):
        raise SandboxViolation(f"sandbox: {event} is not permitted")
    if event == "import" and args and str(args[0]).split(".")[0] in BLOCKED_IMPORTS:
        raise SandboxViolation(f"sandbox: importing {args[0]} is not permitted")


def _parse_memory_mb(argv: list[str]) -> int | None:
    if "--memory-mb" not in argv:
        return None
    value = int(argv[argv.index("--memory-mb") + 1])
    return value if value > 0 else None


def _run(code: str) -> int:
    linecache.cache[CODE_FILENAME] = (
        len(code),
        None,
        code.splitlines(True),
        CODE_FILENAME,
    )
    namespace: dict[str, object] = {
        "__name__": "__main__",
        "__builtins__": __builtins__,
    }
    try:
        exec(compile(code, CODE_FILENAME, "exec"), namespace)
    except SystemExit:
        raise
    except BaseException as exc:
        # Drop this launcher's own frame so the traceback reads as the user's code.
        tb = exc.__traceback__.tb_next if exc.__traceback__ else None
        traceback.print_exception(type(exc), exc, tb)
        return 1
    return 0


def main(argv: list[str]) -> int:
    memory_mb = _parse_memory_mb(argv)
    for name in THREAD_ENV:
        os.environ[name] = "1"
    if memory_mb is not None:
        limit = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    code = sys.stdin.read()
    sys.stdin = open(os.devnull, encoding="utf-8")
    sys.argv = [CODE_FILENAME]
    _install_seccomp()
    sys.addaudithook(_audit_hook)
    return _run(code)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
