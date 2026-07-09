"""Defaults and constants for the code runner verifier.

These constants are used as default_value entries on the registry TaskFieldSchema
declarations. World admins can override them per EvalConfig; the registry resolves
absent fields back to these values.
"""

# Conservative stdlib-only allowlist applied by the AST gate. World admins can
# widen this per EvalConfig via the allowed_imports MULTISELECT field, but cannot
# add anything that isn't actually installed in the grader image.
SAFE_DEFAULT_IMPORTS: list[str] = [
    "csv",
    "json",
    "re",
    "math",
    "statistics",
    "datetime",
    "io",
    "decimal",
    "fractions",
    "collections",
    "itertools",
    "functools",
    "hashlib",
    "base64",
    "html",
    "urllib.parse",
    "string",
    "textwrap",
    "difflib",
    # Read-only inspection of per-app SQLite DBs under ``.apps_data/<svc>/``.
    # Verifiers should open with ``sqlite3.connect("file:...?mode=ro", uri=True)``
    # — the sandbox has no writable disk and read-only mode also bypasses
    # lock-file creation which the unprivileged uid can't perform.
    "sqlite3",
    # Read-only ZIP inspection — needed for xlsx/docx/pptx files which are
    # ZIP archives containing XML. No security risk (no write, no exec).
    "zipfile",
]

# Full menu the EvalConfig form offers as MULTISELECT options. Must be a superset
# of SAFE_DEFAULT_IMPORTS and must align with what the grader Dockerfile installs.
ALL_SUPPORTED_IMPORTS: list[str] = SAFE_DEFAULT_IMPORTS + [
    "pandas",
    "openpyxl",
    "xml.etree.ElementTree",
]

# Always-banned modules — never include in any allowlist regardless of EvalConfig.
# These have well-known escape hatches that an AST gate cannot reliably contain.
PERMANENTLY_BANNED_IMPORTS: frozenset[str] = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "shutil",
        "pathlib",
        "ctypes",
        "pickle",
        "marshal",
        "importlib",
        "imp",
        "builtins",
        "threading",
        "multiprocessing",
        "asyncio",
        "signal",
        "resource",
        "fcntl",
        "platform",
        "tempfile",
    }
)

DEFAULT_TIMEOUT_S: int = 10
MAX_TIMEOUT_S: int = 60
MAX_CODE_LENGTH_CHARS: int = 10_000

# Subtrees the SnapshotCtx whitelist exposes to verifier code, relative to
# the sandbox snapshot root. Used by both the production grading path
# (``main.py``) and the test-run path (``test_run.py``) to set
# ``CODE_RUNNER_ALLOWED_SUBDIRS`` on the sandbox env, AND by ``main.py`` to
# chmod each subtree so uid 65534 can read it. Single source of truth so
# the two execution paths can't silently diverge — adding a new mount here
# enables it for grading AND test-run in one edit.
CTX_ALLOWED_SUBDIRS: tuple[str, ...] = ("filesystem", ".apps_data")

# LiteLLM-format model identifier. PR 2 (codegen endpoint) consumes this; PR 1
# only stores it on the EvalConfig so the form renders correctly.
DEFAULT_CODEGEN_MODEL: str = "anthropic/claude-opus-4-7"
DEFAULT_CODEGEN_TEMPERATURE: float = 0.2
