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
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from loguru import logger

# Default paths to block from user code execution.
# /proc blocks env var exfiltration via /proc/self/environ and path
# traversal via /proc/self/root/*. /sys blocks system config disclosure.
DEFAULT_BLOCKED_PATHS = ["/app", "/.apps_data", "/proc", "/sys"]

# Private package mirror from the install task; not a site directory.
CODE_EXEC_MIRROR = "/usr/local/lib/code_execution"
CODE_EXEC_MIRROR_BIN = f"{CODE_EXEC_MIRROR}/bin"
# Node modules the install task vendors from node_tools/package-lock.json.
CODE_EXEC_NODE_MODULES = f"{CODE_EXEC_MIRROR}/node_modules"

# Every stock LibreOffice launcher (/usr/bin/soffice, localc, ...) ends in
# oosplash, which exits when /proc is unreadable; the shim runs the
# sandbox_bin/soffice wrapper in its place.
LIBREOFFICE_OOSPLASH = "/usr/lib/libreoffice/program/oosplash"
SANDBOX_EXEC_REDIRECTS = f"{LIBREOFFICE_OOSPLASH}={CODE_EXEC_MIRROR_BIN}/soffice"

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
        # Docker-world DB-at-rest decryption master key. The code_execution
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
        # --- Task clock (RL Studio's opt-in libfaketime clock) ---
        #
        # The SPEC only, never the library. On a platform that opts into the
        # task clock, the image bakes libfaketime into /etc/ld.so.preload, so
        # the linker loads it into every process — including the ones we spawn
        # here, which is why the LD_PRELOAD assignment below does not disturb
        # it. What libfaketime then needs is FAKETIME; without it the library
        # is deliberately INERT and the command reports the real date.
        #
        # These have to be allowlisted or the clock stops at this boundary: the
        # server process is on the task's date, but every command the model runs
        # through us is not, so `date` answers today while the rest of the
        # sandbox answers the task's "as of" date. Measured on a backdated
        # platform, which is what motivated this entry.
        #
        # Safe to inherit: the values are a relative offset ("-35700000s") and
        # two literal "1"s, computed server-side from a task field. They name no
        # credential, host or account, and the sandbox boundary — sandbox_fs.so
        # path blocking, or the run-as-user uid — does not consult them. Nor do
        # they grant the model anything it lacks: it can already export FAKETIME
        # inside its own command. Absent entirely on platforms that do not opt
        # in, so this is a no-op everywhere else.
        "FAKETIME",
        # Monotonic clocks stay real, so timeouts, retries and asyncio timers
        # still measure real elapsed time.
        "FAKETIME_DONT_FAKE_MONOTONIC",
        # File mtimes stay real, so a file written "now" does not read back
        # double-shifted.
        "NO_FAKE_STAT",
    }
)

# Default library installation path (under /app/ for Docker multi-stage build compatibility)
DEFAULT_LIBRARY_PATH = "/app/lib/sandbox_fs.so"

# Opt-in: name of an unprivileged user to run model commands as. When set (e.g.
# to "gemini" by a Docker World launch), commands run via ``runuser`` as
# that user INSTEAD of under the LD_PRELOAD sandbox — the boundary becomes
# kernel-enforced file permissions (the user can't read root-owned /app or
# /.apps_data), which the model cannot disable by clearing an env var. Unset
# (the default) preserves the original LD_PRELOAD behavior for every other
# consumer, so this is fully backwards-compatible.
RUN_AS_USER_ENV = "CODE_EXEC_RUN_AS_USER"

# Opt-in: run each model command in its own empty network namespace, so it
# cannot reach the container's MCP servers, gateway or databases on loopback
# (ENV-460). Ignored when the server runs as root without a run-as-user, since
# a root command could nsenter straight back.
ISOLATE_NETWORK_ENV = "CODE_EXEC_ISOLATE_NETWORK"

# Proxy vars bridged into the namespace; everything else has no route out.
_PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")

_NETNS_ROOT = "root"  # root unshares the netns, then drops to the user
_NETNS_USERNS = "userns"  # the user unshares a user+net namespace itself
_NETNS_SELF = "self"  # non-root server unshares a user+net namespace itself

# Positional args only, never interpolated: $1.. are the bridge args (if any)
# followed by the command argv.
_LO_UP = "ip link set lo up 2>/dev/null || true"
_NETNS_SCRIPT = f'{_LO_UP}; exec "$@"'
_NETNS_PROXY_SCRIPT = (
    f"{_LO_UP}; "
    'sock=$1; port=$2; shift 2; hex=$(printf "%04X" "$port"); '
    'socat TCP-LISTEN:"$port",bind=127.0.0.1,fork,reuseaddr UNIX-CONNECT:"$sock" </dev/null >/dev/null 2>&1 & b=$!; '
    'i=0; until grep -q ":$hex 00000000:0000 0A" /proc/net/tcp 2>/dev/null; do '
    "i=$((i+1)); if [ $i -gt 100 ]; then kill $b 2>/dev/null; "
    'echo "code-exec: egress proxy bridge failed to start" >&2; exit 125; fi; '
    "sleep 0.05; done; "
    '"$@"; rc=$?; kill $b 2>/dev/null; exit $rc'
)

# The userns variant keeps namespace caps for `ip`; the command must not.
_DROP_CAPS = [
    "setpriv",
    "--inh-caps=-all",
    "--ambient-caps=-all",
    "--bounding-set=-all",
    "--",
]

# Keyed by run-as-user; None is the non-root server itself.
_netns_mode_cache: dict[str | None, str] = {}

_USERNS_UNSHARE = [
    "unshare",
    "--user",
    "--map-current-user",
    "--keep-caps",  # ip needs CAP_NET_ADMIN in the new netns
    "--net",
    "--",
]

# Basename of the scratch HOME handed to sandboxed code (see
# ``_resolve_sandbox_home``). Used under $STATE_LOCATION and at the filesystem
# root, so it must not collide with a real directory name.
_SANDBOX_HOME_DIRNAME = "sandbox-home"


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


def _is_under_blocked_path(path: str, blocked_paths: list[str]) -> bool:
    """Whether ``path`` IS a blocked path or lives underneath one.

    Mirrors the prefix rule the LD_PRELOAD shim applies in ``sandbox_fs.c``: a
    blocked entry matches only when the next character is ``\\0`` or ``/``, so a
    blocked ``/app`` does not match ``/appdata``. Both the lexically normalized
    path and its symlink-resolved form are checked, so a candidate that is a
    symlink into a blocked tree is rejected here too (the shim would deny it at
    runtime regardless).
    """
    forms = {os.path.normpath(path)}
    try:
        forms.add(os.path.realpath(path))
    except OSError:  # realpath rarely raises; treat as "only the literal form"
        pass

    for blocked in blocked_paths:
        normalized = os.path.normpath(blocked)
        prefix = normalized.rstrip("/") + "/"
        if any(form == normalized or form.startswith(prefix) for form in forms):
            return True
    return False


def _sandbox_home_candidates() -> list[str]:
    """Ordered candidate directories for the sandboxed process's HOME.

    ``$STATE_LOCATION/sandbox-home`` stays first so hosts that point
    STATE_LOCATION at a writable, non-blocked directory keep the behavior
    introduced for the read-only-``/tmp`` compute. ``/tmp`` is next (the
    original default), then ``/var/tmp``, then ``/sandbox-home`` as a last
    resort for hosts where ``/tmp`` is not writable.
    """
    candidates = []
    state_location = os.environ.get("STATE_LOCATION", "")
    if state_location:
        candidates.append(os.path.join(state_location, _SANDBOX_HOME_DIRNAME))
    candidates.extend(["/tmp", "/var/tmp", f"/{_SANDBOX_HOME_DIRNAME}"])
    return candidates


def _prepare_writable_dir(path: str) -> bool:
    """Create ``path`` if needed; report whether it ends up writable."""
    already_existed = os.path.isdir(path)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logger.debug(f"Sandbox home candidate {path!r} not creatable: {e}")
        return False

    if not already_existed:
        # Sticky + world-writable, like /tmp. The sandboxed command may run
        # under a different uid than this server (run-as-user mode, or a
        # non-root host), so it must be able to write here; the sticky bit
        # keeps one uid from deleting or renaming another uid's files.
        try:
            os.chmod(path, 0o1777)
        except OSError as e:
            logger.debug(f"Could not chmod sandbox home {path!r}: {e}")

    return os.access(path, os.W_OK)


def _resolve_sandbox_home(blocked_paths: list[str]) -> str:
    """Pick a HOME/PYTHONUSERBASE for sandboxed code: writable, never blocked.

    A home inside a blocked path breaks ``pip`` user-scheme installs: pip's
    ``os.makedirs`` of ``$PYTHONUSERBASE/lib/pythonX.Y/site-packages`` recurses
    up to the blocked root and the shim denies it, surfacing as
    ``[Errno 13] Permission denied: '/.apps_data'``. Candidates that fall inside
    ``blocked_paths`` are therefore skipped, regardless of how STATE_LOCATION is
    configured.
    """
    for candidate in _sandbox_home_candidates():
        if _is_under_blocked_path(candidate, blocked_paths):
            logger.debug(
                f"Skipping sandbox home {candidate!r}: inside a blocked path "
                f"({blocked_paths})"
            )
            continue
        if _prepare_writable_dir(candidate):
            return candidate

    # Never raise: code_exec must keep working even if every candidate failed.
    logger.warning(
        f"No writable sandbox home outside blocked paths {blocked_paths}; "
        "falling back to /tmp (pip user installs may fail)"
    )
    return "/tmp"


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
    # The chosen directory must be writable AND outside ``paths`` — a home inside
    # a blocked tree (e.g. $STATE_LOCATION=/.apps_data/code_execution) makes pip
    # user installs fail with EACCES on the blocked root.
    sandbox_home = _resolve_sandbox_home(paths)
    env["HOME"] = sandbox_home
    env["PYTHONUSERBASE"] = sandbox_home

    # Drop the server's PYTHONPATH; point it at the private mirror.
    env.pop("PYTHONPATH", None)
    env["PYTHONPATH"] = CODE_EXEC_MIRROR

    # Same for node: `require('pptxgenjs')` from a script anywhere resolves the
    # vendored modules. Assigned, never inherited (NODE_PATH is not on the
    # allowlist), so the server's environment cannot redirect it.
    env["NODE_PATH"] = CODE_EXEC_NODE_MODULES

    # Prefer system Python and the mirror's bin over mise/venv paths.
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
        CODE_EXEC_MIRROR_BIN,
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
    env["SANDBOX_EXEC_REDIRECTS"] = SANDBOX_EXEC_REDIRECTS

    # Set sandbox-specific environment variables.
    #
    # Assigned, not appended: this must be the shim the model's code runs under,
    # and an inherited LD_PRELOAD is not ours to preserve. Nothing that has to
    # load in every process should rely on this variable — RL Studio's task
    # clock, for one, is loaded by /etc/ld.so.preload instead, which a child's
    # environment cannot override. See FAKETIME in _SANDBOX_ENV_ALLOWLIST.
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
    the server to be root, which it is in the Docker World launch). We deliberately avoid
    the login form ``runuser -l``: an end-to-end test confirmed its login shell
    **drops** proxy/TLS env vars (e.g. HTTP_PROXY) that ``pip`` / network installs
    need. The non-login form preserves the env we pass — and that env is already
    allowlist-scrubbed + secret-scrubbed by ``build_sandbox_env``, so nothing
    sensitive leaks while proxy/TLS/PATH survive. CWD is set via ``Popen(cwd=...)``.
    """
    return ["runuser", "-u", user, "--", "sh", "-c", command]


def network_isolation_requested() -> bool:
    """Whether ``CODE_EXEC_ISOLATE_NETWORK`` asks for a per-command netns."""
    value = os.environ.get(ISOLATE_NETWORK_ENV, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _netns_modes(user: str | None) -> tuple[str, ...]:
    return (_NETNS_SELF,) if user is None else (_NETNS_ROOT, _NETNS_USERNS)


def _netns_prefix(mode: str, user: str | None) -> list[str]:
    if mode == _NETNS_ROOT:
        return ["unshare", "--net", "--"]
    if mode == _NETNS_SELF:
        return list(_USERNS_UNSHARE)
    assert user is not None
    return ["runuser", "-u", user, "--", *_USERNS_UNSHARE]


def build_netns_argv(
    argv: list[str],
    user: str | None,
    mode: str,
    proxy_bridge: tuple[str, int] | None = None,
    preload: str | None = None,
) -> list[str]:
    """``argv`` run as ``user`` (None: this process's uid) in a fresh netns, loopback only.

    ``proxy_bridge`` is ``(unix_socket, port)``: the namespace gets a listener on
    ``127.0.0.1:port`` forwarding to the socket, and nothing else. ``preload``
    is set as ``LD_PRELOAD`` on the command only, never on the wrapper (the shim
    blocks /proc, which unshare needs for uid_map).
    """
    if preload is not None:
        argv = ["env", f"LD_PRELOAD={preload}", *argv]
    if mode == _NETNS_ROOT:
        assert user is not None
        inner = ["runuser", "-u", user, "--", *argv]
    else:
        inner = [*_DROP_CAPS, *argv]
    if proxy_bridge is None:
        script, bridge_args = _NETNS_SCRIPT, []
    else:
        script, bridge_args = (
            _NETNS_PROXY_SCRIPT,
            [proxy_bridge[0], str(proxy_bridge[1])],
        )
    return [
        *_netns_prefix(mode, user),
        "sh",
        "-c",
        script,
        "cx-netns",
        *bridge_args,
        *inner,
    ]


def _resolve_netns_mode(user: str | None) -> str | None:
    """First namespace variant this runtime permits, or ``None``.

    Root ``unshare --net`` needs CAP_SYS_ADMIN (absent from Docker's default
    set); the user-namespace variants need only unprivileged userns, no caps.
    """
    if user in _netns_mode_cache:
        return _netns_mode_cache[user]
    for mode in _netns_modes(user):
        probe = [*_netns_prefix(mode, user), "ip", "link", "set", "lo", "up"]
        try:
            result = subprocess.run(probe, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning(f"Network namespace probe {mode!r} failed: {e}")
            continue
        if result.returncode == 0:
            logger.info(f"Model commands isolated with {mode!r} network namespace")
            _netns_mode_cache[user] = mode
            return mode
        logger.warning(
            f"Network namespace probe {mode!r} failed: {result.stderr.strip()}"
        )
    return None


def _parse_proxy(value: str) -> tuple[str, int, str]:
    """``(host, port, url)`` for a proxy var; ``url`` always carries a scheme."""
    url = value if "://" in value else f"http://{value}"
    parts = urlsplit(url)
    if parts.scheme == "https":
        raise ValueError(
            "https:// egress proxies are not supported (the bridge serves the "
            "proxy on 127.0.0.1, where its TLS certificate cannot verify)"
        )
    if not parts.hostname:
        raise ValueError(f"proxy {value!r} has no host")
    return parts.hostname, parts.port or 80, url


def _proxy_endpoint(env: dict[str, str]) -> tuple[str, int] | None:
    """The single egress proxy named by the sandbox env, or ``None``."""
    endpoints = {
        _parse_proxy(env[name].strip())[:2]
        for name in _PROXY_ENV_NAMES
        if env.get(name, "").strip()
    }
    if len(endpoints) > 1:
        raise ValueError(f"multiple egress proxies are not supported: {endpoints}")
    return next(iter(endpoints), None)


def _point_proxy_env_at_bridge(env: dict[str, str], port: int) -> None:
    for name in _PROXY_ENV_NAMES:
        value = env.get(name, "").strip()
        if not value:
            continue
        parts = urlsplit(_parse_proxy(value)[2])
        userinfo = parts.netloc.rpartition("@")[0]
        netloc = f"{userinfo}@127.0.0.1:{port}" if userinfo else f"127.0.0.1:{port}"
        rewritten = parts._replace(netloc=netloc).geturl()
        env[name] = rewritten if "://" in value else rewritten.removeprefix("http://")


def _start_proxy_bridge(
    endpoint: tuple[str, int], user: str | None
) -> tuple[subprocess.Popen[bytes], str]:
    """Host-side ``socat`` from a ``user``-only (None: own uid) Unix socket to the proxy."""
    host, port = endpoint
    sock_dir = tempfile.mkdtemp(prefix="cx-egress-")
    try:
        sock = os.path.join(sock_dir, "proxy.sock")
        listen = f"UNIX-LISTEN:{sock},fork,mode=600"
        if user is not None:
            shutil.chown(sock_dir, user=user)
            listen += f",user={user}"
        target = f"TCP6:[{host}]:{port}" if ":" in host else f"TCP:{host}:{port}"
        proc = subprocess.Popen(
            ["socat", listen, target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, LookupError):
        shutil.rmtree(sock_dir, ignore_errors=True)
        raise
    deadline = time.monotonic() + 5
    while not os.path.exists(sock):
        if proc.poll() is not None or time.monotonic() > deadline:
            _stop_proxy_bridge(proc, sock_dir)
            raise RuntimeError("socat did not create the bridge socket")
        time.sleep(0.02)
    return proc, sock_dir


def _stop_proxy_bridge(proc: subprocess.Popen[bytes], sock_dir: str) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # socat's per-connection forks too
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    shutil.rmtree(sock_dir, ignore_errors=True)


def _isolation_error(reason: str) -> SandboxResult:
    msg = f"Network isolation is enabled ({ISOLATE_NETWORK_ENV}) but {reason}; refusing to run the command without it."
    logger.error(msg)
    return SandboxResult(stdout="", stderr="", return_code=-1, error=msg)


def _run_network_isolated(
    argv: list[str],
    user: str | None,
    timeout: int,
    env: dict[str, str],
    cwd: str | None,
    input_text: str | None,
) -> SandboxResult:
    """Run ``argv`` as ``user`` (None: own uid) in an empty netns; fail closed if one can't be made."""
    preload = env.pop("LD_PRELOAD", None)
    mode = _resolve_netns_mode(user)
    if mode is None:
        return _isolation_error(
            "no network namespace could be created (unshare/ip missing or not permitted)"
        )
    try:
        endpoint = _proxy_endpoint(env)
    except ValueError as e:
        return _isolation_error(str(e))
    if endpoint is None:
        return _spawn_and_collect(
            build_netns_argv(argv, user, mode, preload=preload),
            timeout,
            env,
            cwd=cwd,
            **_stdin_kwargs(input_text),
        )
    try:
        bridge, sock_dir = _start_proxy_bridge(endpoint, user)
    except (OSError, LookupError, RuntimeError) as e:
        return _isolation_error(f"the egress proxy bridge failed to start: {e}")
    try:
        _point_proxy_env_at_bridge(env, endpoint[1])
        sock = os.path.join(sock_dir, "proxy.sock")
        return _spawn_and_collect(
            build_netns_argv(argv, user, mode, (sock, endpoint[1]), preload),
            timeout,
            env,
            cwd=cwd,
            **_stdin_kwargs(input_text),
        )
    finally:
        _stop_proxy_bridge(bridge, sock_dir)


def _spawn_and_collect(
    argv: list[str],
    timeout: int,
    env: dict[str, str],
    cwd: str | None,
    input_text: str | None = None,
) -> SandboxResult:
    """Run ``argv`` with timeout + process-group cleanup, returning a result.

    Uses ``start_new_session=True`` so the whole process tree (shell + children)
    can be killed on timeout. Shared by the LD_PRELOAD and run-as-user paths.
    ``input_text`` is written to the child's stdin; when ``None`` stdin is inherited.
    """
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Model commands legitimately emit bytes that are not valid utf-8:
            # `cat` an xlsx, `curl` a binary URL without -o, or a latin-1 text
            # file. Strict decoding raises UnicodeDecodeError *inside*
            # communicate(), so it lands in the generic handler below and the
            # caller gets error=... with stdout/stderr empty — every byte the
            # command produced is discarded. Replacing the undecodable bytes
            # keeps the output; the tool layer decides whether it is fit to show
            # (see _replace_binary_streams in tools/code_exec.py).
            errors="replace",
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
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
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
    return run_sandboxed_argv(
        ["sh", "-c", command],
        timeout,
        working_dir=working_dir,
        blocked_paths=blocked_paths,
        library_path=library_path,
        debug=debug,
    )


def run_sandboxed_argv(
    argv: list[str],
    timeout: int,
    working_dir: str = "/filesystem",
    blocked_paths: list[str] | None = None,
    library_path: str = DEFAULT_LIBRARY_PATH,
    debug: bool = False,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> SandboxResult:
    """Run ``argv`` (no shell) with the same isolation modes as ``run_sandboxed_command``."""
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
            extra_env=extra_env,
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
        logger.debug(f"Running command as user {run_as_user!r}: {argv}")
        if network_isolation_requested():
            return _run_network_isolated(
                argv, run_as_user, timeout, env, working_dir, input_text
            )
        return _spawn_and_collect(
            ["runuser", "-u", run_as_user, "--", *argv],
            timeout,
            env,
            cwd=working_dir,
            **_stdin_kwargs(input_text),
        )

    isolate = network_isolation_requested()
    if isolate and os.geteuid() == 0:
        isolate = False
        logger.warning(
            f"{ISOLATE_NETWORK_ENV} ignored: the server runs as root without "
            f"{RUN_AS_USER_ENV}, so a command could re-enter the host network namespace"
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
        extra_env=extra_env,
    )

    logger.debug(f"Running sandboxed command: {argv}")
    logger.debug(f"Working directory: {working_dir}")
    logger.debug(f"Blocked paths: {blocked_paths or DEFAULT_BLOCKED_PATHS}")

    if isolate:
        return _run_network_isolated(argv, None, timeout, env, working_dir, input_text)
    return _spawn_and_collect(
        argv, timeout, env, cwd=working_dir, **_stdin_kwargs(input_text)
    )


def _stdin_kwargs(input_text: str | None) -> dict[str, str]:
    # Omitted when unset so the shell path calls _spawn_and_collect exactly as before.
    return {} if input_text is None else {"input_text": input_text}
