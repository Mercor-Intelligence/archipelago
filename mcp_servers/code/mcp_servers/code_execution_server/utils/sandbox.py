"""
sandbox.py - LD_PRELOAD-based filesystem sandboxing for code execution

This module provides sandboxed execution using the sandbox_fs.so LD_PRELOAD library.
It intercepts libc filesystem calls to block access to specified paths while allowing
normal operations elsewhere (including package installation).

Usage:
    from utils.sandbox import run_sandboxed_command

    result = run_sandboxed_command(
        command="python script.py",
        timeout=30,
        blocked_paths=["/app", "/.apps_data"],
    )
"""

import os
import signal
import subprocess
from dataclasses import dataclass

from loguru import logger

# Default paths to block from user code execution.
# /proc blocks env var exfiltration via /proc/self/environ and path
# traversal via /proc/self/root/*. /sys blocks system config disclosure.
DEFAULT_BLOCKED_PATHS = ["/app", "/.apps_data", "/proc", "/sys"]

# Substrings in environment variable names that indicate sensitive values.
# If any of these appear (case-insensitive) in a variable name, it is scrubbed.
#
# This is a denylist, so it is inherently incomplete: a sandboxed `env` dump
# revealed real secrets/infra vars that slipped past the original keyword set
# (e.g. DATADOG_APP_KEY — "APP_KEY" is one letter off from "API_KEY" — plus
# Modal/S3 recon vars). The additions below close those families. We avoid
# over-broad tokens like a bare "KEY"/"ARN"/"ENV"/"DD_" that would scrub benign
# vars; each entry is scoped enough to not match the control vars we rely on
# (PATH, HOME, LANG, LD_PRELOAD, SANDBOX_BLOCKED_PATHS, PYTHONUSERBASE, ...).
_SENSITIVE_ENV_SUBSTRINGS = (
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "TOKEN",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "API_KEY",
    "APIKEY",
    "ACCESS_KEY",  # generalize AWS-style access keys
    "APP_KEY",  # e.g. DATADOG_APP_KEY (does NOT match "API_KEY")
    "SIGNING",  # e.g. API_SIGNING_PUBLIC_KEY / request-signing keys
    "SESSION",  # session tokens / cookies
    "_ARN",  # e.g. MODAL_OIDC_ROLE_ARN (underscore avoids LEARN/WARNING)
    "OIDC",  # OIDC role / token config
    "MODAL_",  # Modal infra: MODAL_TASK_ID, MODAL_CLOUD_PROVIDER, MODAL_OIDC_*
    "DATADOG",  # Datadog keys/config (DATADOG_APP_KEY, DATADOG_API_KEY, ...)
    "S3_",  # S3 bucket/region/prefix recon (S3_DEFAULT_REGION, S3_SNAPSHOTS_PREFIX)
)

# Exact environment variable names to always scrub, even if they don't match
# the substring patterns above (e.g. they contain connection strings or key IDs).
_SENSITIVE_ENV_NAMES = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "DATABASE_URL",
        "REDIS_URL",
        "MONGO_URI",
        "MONGODB_URI",
        "CONNECTION_STRING",
        "DSN",
        # GDM docker-world DB-at-rest decryption master key. The code_execution
        # server inherits it from the container environment, and it must never
        # reach sandboxed model code — otherwise the model could decrypt the
        # encrypted app SQLite DBs (read them directly or via the public shim).
        # Doesn't match any substring above, so it must be listed explicitly.
        "DB_ENC_KEY",
    }
)

# Allowlist of environment variable NAMES permitted to pass from the server's
# environment into sandboxed model code. This is the primary, fail-closed
# control: ONLY these survive, so any secret or infra var the server happens to
# hold — current or added in the future — is dropped by default. The keyword
# denylist above is kept as a second defense-in-depth pass (in case an
# allowlisted name ever carries a secret value), but it is no longer what we
# rely on. A name listed here that isn't set in the environment costs nothing
# (we only copy vars that actually exist), so the list can safely be generous
# with known-safe, non-secret names without risking that a task breaks.
#
# The core runtime/locale entries mirror archipelago's code_execution verifier
# allowlist (_SANDBOX_ENV_ALLOWLIST in
# grading/runner/evals/code_execution/main.py), which already runs
# agent-submitted code under this exact allowlist in production. The live MCP
# sandbox additionally permits `pip install`, network downloads, and plotting
# (unlike the grader's offline test run), so it extends the set with the TLS /
# proxy / pip-config / scientific-stack vars those operations need.
_SANDBOX_ENV_ALLOWLIST = frozenset(
    {
        # --- Core runtime / locale (mirror archipelago grader) ---
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TZ",
        "PYTHONHASHSEED",
        "PYTHON_VERSION",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        # --- TLS / CA bundles (needed for HTTPS pip/uv installs) ---
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "PIP_CERT",
        # --- Egress proxy (dropping these kills all network access) ---
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        # --- pip / uv behavior (non-secret install config) ---
        "PIP_ROOT_USER_ACTION",
        "PIP_DEFAULT_TIMEOUT",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_NO_CACHE_DIR",
        "PIP_DISABLE_PIP_VERSION_CHECK",
        "UV_INDEX_URL",
        "UV_DEFAULT_INDEX",
        # --- Scientific-stack threading knobs (perf / determinism) ---
        "OPENBLAS_NUM_THREADS",
        "BLIS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        # --- Headless plotting ---
        "MPLBACKEND",
    }
)

# Default library installation path (under /app/ for Docker multi-stage build compatibility)
DEFAULT_LIBRARY_PATH = "/app/lib/sandbox_fs.so"

# Opt-in: name of an unprivileged user to run model commands as. When set (e.g.
# to "gemini" by a GDM Docker World launch), commands run via ``runuser`` as
# that user INSTEAD of under the LD_PRELOAD sandbox — the boundary becomes
# kernel-enforced file permissions (the user can't read root-owned /app or
# /.apps_data), which the model cannot disable by clearing an env var. Unset
# (the default) preserves the original LD_PRELOAD behavior for every other
# consumer, so this is fully backwards-compatible.
RUN_AS_USER_ENV = "CODE_EXEC_RUN_AS_USER"


@dataclass
class SandboxResult:
    """Result of a sandboxed command execution."""

    stdout: str
    stderr: str
    return_code: int
    timed_out: bool = False
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.return_code == 0 and not self.timed_out and self.error is None


def verify_sandbox_library_available(library_path: str = DEFAULT_LIBRARY_PATH) -> None:
    """Verify sandbox_fs.so library is available. Call at server startup.

    Raises:
        RuntimeError: If the sandbox library is not found.
    """
    if not os.path.exists(library_path):
        raise RuntimeError(
            f"sandbox_fs.so is required for sandboxed code execution but was not found at {library_path}. "
            "Please compile and install it first using: "
            "mkdir -p /app/lib && gcc -shared -fPIC -O2 -o /app/lib/sandbox_fs.so sandbox_fs.c -ldl -lpthread"
        )
    logger.info(f"sandbox_fs.so library found at {library_path} - sandboxing enabled")


def _is_sensitive_env_var(name: str) -> bool:
    """Check if an environment variable name likely contains sensitive data.

    Uses a combination of substring matching and exact name matching to
    identify variables that may contain API keys, passwords, tokens, or
    connection strings that should not be passed to sandboxed code.
    """
    upper = name.upper()
    if upper in _SENSITIVE_ENV_NAMES:
        return True
    for pattern in _SENSITIVE_ENV_SUBSTRINGS:
        if pattern in upper:
            return True
    return False


def _scrub_sensitive_env_vars(env: dict[str, str]) -> dict[str, str]:
    """Remove environment variables that likely contain secrets.

    Returns a new dict with sensitive variables removed. This is a defense-in-depth
    measure: even if /proc is blocked (preventing filesystem reads of environ),
    Python's os.environ reads from process memory and would still expose inherited
    secrets without this scrubbing.
    """
    scrubbed = {k: v for k, v in env.items() if not _is_sensitive_env_var(k)}
    removed = set(env.keys()) - set(scrubbed.keys())
    if removed:
        logger.debug(f"Scrubbed {len(removed)} sensitive env vars: {sorted(removed)}")
    return scrubbed


def _filter_to_allowlist(env: dict[str, str]) -> dict[str, str]:
    """Keep only env vars whose names are on ``_SANDBOX_ENV_ALLOWLIST``.

    Fail-closed: anything not explicitly allowlisted — including secrets and
    infra vars we never anticipated — is dropped before reaching sandboxed
    model code. Logs only the NAMES of dropped vars, never their values.
    """
    filtered = {k: v for k, v in env.items() if k in _SANDBOX_ENV_ALLOWLIST}
    dropped = set(env.keys()) - set(filtered.keys())
    if dropped:
        logger.debug(
            f"Dropped {len(dropped)} non-allowlisted env vars: {sorted(dropped)}"
        )
    return filtered


def build_sandbox_env(
    blocked_paths: list[str] | None = None,
    library_path: str = DEFAULT_LIBRARY_PATH,
    debug: bool = False,
    inherit_env: bool = True,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build environment variables for sandboxed execution.

    Args:
        blocked_paths: List of filesystem paths to block (default: ["/app", "/.apps_data"])
        library_path: Path to the sandbox_fs.so library
        debug: Enable debug logging in the sandbox library
        inherit_env: Whether to inherit current environment variables
        extra_env: Additional environment variables to set

    Returns:
        Dictionary of environment variables for the subprocess.
    """
    paths = blocked_paths or DEFAULT_BLOCKED_PATHS

    if inherit_env:
        # Fail-closed: allowlist first (only known-safe names survive), then run
        # the keyword denylist as a second defense-in-depth pass in case an
        # allowlisted name ever carries a secret value.
        env = _scrub_sensitive_env_vars(_filter_to_allowlist(os.environ.copy()))
    else:
        # Minimal environment
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }

    # Override HOME and Python user paths to avoid PermissionError when pip
    # scans the original HOME (e.g. /root) for user-site packages. The sandboxed
    # process may not have read access to the server's HOME directory.
    # PYTHONUSERBASE controls where Python looks for user-site packages and where
    # pip --user installs to. Setting both ensures pip install works correctly.
    #
    # Use ``$STATE_LOCATION/sandbox-home`` (mirrors injected_errors.py's
    # STATE_LOCATION convention) so the scratch dir lives somewhere the MCP
    # server already manages and the host writes to — required on hosts that
    # mount ``/tmp`` read-only (e.g. GDM xbox). Falls back to ``/tmp`` when
    # STATE_LOCATION is unset so existing dev/test setups keep working.
    state_location = os.environ.get("STATE_LOCATION", "")
    sandbox_home = (
        os.path.join(state_location, "sandbox-home") if state_location else "/tmp"
    )
    os.makedirs(sandbox_home, exist_ok=True)
    env["HOME"] = sandbox_home
    env["PYTHONUSERBASE"] = sandbox_home

    # Remove PYTHONPATH inherited from the MCP server (e.g. /app or venv paths).
    # These can cause pip to scan blocked/inaccessible directories. The system
    # Python finds pre-installed packages via its own site-packages without
    # PYTHONPATH. Users can set PYTHONPATH explicitly in their commands if needed.
    env.pop("PYTHONPATH", None)

    # Ensure system Python is used for user code execution, not mise/venv Python.
    # Packages are installed to system Python (/usr/bin/python3), so we need to
    # prioritize /usr/bin and /usr/local/bin in PATH.
    # Filter out mise and venv Python paths from PATH.
    current_path = env.get("PATH", "/usr/bin:/bin")
    path_parts = current_path.split(":")
    filtered_parts = [
        p
        for p in path_parts
        if not any(
            exclude in p for exclude in [".venv", "mise/installs", ".local/share/mise"]
        )
    ]
    # Ensure system paths are first
    system_paths = [
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/local/sbin",
        "/usr/sbin",
        "/sbin",
    ]
    for sp in reversed(system_paths):
        if sp in filtered_parts:
            filtered_parts.remove(sp)
        filtered_parts.insert(0, sp)
    env["PATH"] = ":".join(filtered_parts)

    # Set sandbox-specific environment variables
    env["LD_PRELOAD"] = library_path
    env["SANDBOX_BLOCKED_PATHS"] = ":".join(paths)

    if debug:
        env["SANDBOX_DEBUG"] = "1"

    # Add any extra environment variables
    if extra_env:
        env.update(extra_env)

    return env


def configured_run_as_user() -> str | None:
    """Unprivileged user to drop model commands to, or ``None`` if unset.

    Reads ``CODE_EXEC_RUN_AS_USER``. Empty/unset → ``None`` → original
    LD_PRELOAD behavior (so all existing consumers are unaffected).
    """
    user = os.environ.get(RUN_AS_USER_ENV, "").strip()
    return user or None


def build_run_as_user_argv(command: str, user: str) -> list[str]:
    """Wrap ``command`` to run as ``user`` via ``runuser`` (non-login).

    Uses the non-login form ``runuser -u <user> -- sh -c "<command>"`` (requires
    the server to be root, which it is in the GDM launch). We deliberately avoid
    the login form ``runuser -l``: an end-to-end test confirmed its login shell
    **drops** proxy/TLS env vars (e.g. HTTP_PROXY) that ``pip`` / network installs
    need. The non-login form preserves the env we pass — and that env is already
    allowlist-scrubbed + secret-scrubbed by ``build_sandbox_env``, so nothing
    sensitive leaks while proxy/TLS/PATH survive. CWD is set via ``Popen(cwd=...)``.
    """
    return ["runuser", "-u", user, "--", "sh", "-c", command]


def _spawn_and_collect(
    argv: list[str], timeout: int, env: dict[str, str], cwd: str | None
) -> SandboxResult:
    """Run ``argv`` with timeout + process-group cleanup, returning a result.

    Uses ``start_new_session=True`` so the whole process tree (shell + children)
    can be killed on timeout. Shared by the LD_PRELOAD and run-as-user paths.
    """
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
    except (FileNotFoundError, OSError) as e:
        # The launcher itself couldn't start (e.g. `runuser` not installed, or
        # cwd missing). Fail closed with an error result rather than crashing.
        logger.error(f"Failed to spawn sandboxed process {argv[:1]}: {e}")
        return SandboxResult(stdout="", stderr="", return_code=-1, error=str(e))

    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return SandboxResult(
            stdout=stdout,
            stderr=stderr,
            return_code=process.returncode,
        )
    except subprocess.TimeoutExpired:
        # Kill the entire process group, not just the direct child, so the
        # shell and all children terminate.
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            process.kill()  # process group may already be gone
        try:
            stdout, stderr = process.communicate()
        except Exception:
            stdout, stderr = "", ""
        return SandboxResult(
            stdout=stdout or "",
            stderr=stderr or "",
            return_code=-1,
            timed_out=True,
            error=f"Command timed out after {timeout} seconds",
        )
    except Exception as e:
        logger.exception("Error running sandboxed command")
        stdout, stderr = "", ""
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass  # already terminated
        # Reap to prevent a zombie; capture any partial output for debugging.
        try:
            out, err = process.communicate(timeout=1)
            stdout = out or ""
            stderr = err or ""
        except Exception:
            try:
                process.wait(timeout=1)
            except Exception:
                pass  # best-effort cleanup
        return SandboxResult(
            stdout=stdout,
            stderr=stderr,
            return_code=-1,
            error=str(e),
        )


def run_sandboxed_command(
    command: str,
    timeout: int,
    working_dir: str = "/filesystem",
    blocked_paths: list[str] | None = None,
    library_path: str = DEFAULT_LIBRARY_PATH,
    debug: bool = False,
) -> SandboxResult:
    """Run a shell command with filesystem isolation.

    Two modes, selected by the ``CODE_EXEC_RUN_AS_USER`` env var:

    * **run-as-user** (env set): the command runs as an unprivileged user via
      ``runuser``. Isolation is kernel-enforced file permissions — the user
      can't read root-owned ``/app`` / ``/.apps_data`` — which, unlike the
      LD_PRELOAD shim, the model cannot disable by clearing an env var. No
      sandbox library is required in this mode.
    * **LD_PRELOAD** (env unset, the default): the original behavior — load
      ``sandbox_fs.so`` to block configured paths, failing closed if the
      library is missing. Byte-identical to before for existing consumers.

    Uses ``start_new_session=True`` for clean timeout handling.
    """
    run_as_user = configured_run_as_user()

    if run_as_user:
        # Kernel-enforced uid boundary instead of the LD_PRELOAD shim. Build the
        # child env through the same allowlist/scrub path, then strip the
        # LD_PRELOAD sandbox vars (not used here). No fail-closed library check:
        # the boundary is the unprivileged user, not the shim.
        env = build_sandbox_env(
            blocked_paths=blocked_paths,
            library_path=library_path,
            debug=debug,
            inherit_env=True,
        )
        env.pop("LD_PRELOAD", None)
        env.pop("SANDBOX_BLOCKED_PATHS", None)
        # Repoint HOME / PYTHONUSERBASE at the dropped user's own home.
        # build_sandbox_env derives these from $STATE_LOCATION (e.g.
        # /.apps_data/sandbox-home), which is root-owned and unreadable by the
        # unprivileged user — so pip --user, ``~`` expansion, and other $HOME
        # writes would fail. ``/home/<user>`` is created (and owned by the user)
        # by ``useradd -m`` at build time, so it is both readable and writable.
        user_home = f"/home/{run_as_user}"
        env["HOME"] = user_home
        env["PYTHONUSERBASE"] = user_home
        logger.debug(f"Running command as user {run_as_user!r}: {command}")
        return _spawn_and_collect(
            build_run_as_user_argv(command, run_as_user),
            timeout,
            env,
            cwd=working_dir,
        )

    # Fail-closed: verify sandbox library exists before every execution.
    # If missing, the command would run unsandboxed (LD_PRELOAD silently fails).
    if not os.path.exists(library_path):
        error_msg = (
            f"Sandbox library not found at {library_path}. "
            "Refusing to execute command without sandboxing."
        )
        logger.error(error_msg)
        return SandboxResult(
            stdout="",
            stderr="",
            return_code=-1,
            error=error_msg,
        )

    env = build_sandbox_env(
        blocked_paths=blocked_paths,
        library_path=library_path,
        debug=debug,
        inherit_env=True,
    )

    logger.debug(f"Running sandboxed command: {command}")
    logger.debug(f"Working directory: {working_dir}")
    logger.debug(f"Blocked paths: {blocked_paths or DEFAULT_BLOCKED_PATHS}")

    return _spawn_and_collect(["sh", "-c", command], timeout, env, cwd=working_dir)
