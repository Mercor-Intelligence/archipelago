"""Import-allowlist resolution for db_code_verifier.

db_code_verifier shares the llm_code_verifier AST gate and import lists.
This module adds the ``allow_legacy_fields`` switch that selects between:

* **Legacy mode** (the default when the flag is absent): the per-world
  ``allowed_imports`` EvalConfig field drives the gate, unioned with the
  blessed set (matching the export/QC resolver). Existing worlds keep working.

* **New mode** (``allow_legacy_fields`` explicitly ``False``): one canonical
  "blessed" library set is always on. It is enforced by the AST gate AND
  enumerated verbatim in the codegen system prompt, so the model is always
  told exactly the libraries it may use and the two can't drift. There is no
  per-world import configuration in this mode.

The blessed set is intentionally identical to llm_code_verifier's
``ALL_SUPPORTED_IMPORTS`` so both code-verifier kinds share one vetted set of
libraries that are actually installed in the grader image. The permanent-ban
list (os, subprocess, socket, …) is untouched and remains the security floor.
"""

from __future__ import annotations

from typing import Any

from runner.evals.llm_code_verifier.config import ALL_SUPPORTED_IMPORTS

# Canonical "blessed" import set used in new (non-legacy) mode. Single source
# for the AST gate; the server-side codegen prompt mirrors this list and a
# parity test asserts the two match.
DB_CODE_VERIFIER_BLESSED_IMPORTS: list[str] = list(ALL_SUPPORTED_IMPORTS)

# Hardcoded max code length for db_code_verifier. The old per-world
# ``max_code_length_chars`` field added config surface for no real benefit, so
# it's gone — a single generous cap is plenty for a ``def check(ctx)`` body and
# only ever bounds pathological input. Raising the cap can never reject code an
# existing world's verifier already passed with, so this applies in all modes.
DB_CODE_VERIFIER_MAX_CODE_LENGTH: int = 50_000

# Must match the registry field defaults; a parity test pins them.
DB_CODE_VERIFIER_DEFAULT_TIMEOUT_S: int = 300
DB_CODE_VERIFIER_MAX_TIMEOUT_S: int = 1200
# Under the 120s Modal cap on the Test button's function.
DB_CODE_VERIFIER_TEST_RUN_MAX_TIMEOUT_S: int = 100


def is_legacy_fields_mode(eval_config_values: dict[str, Any]) -> bool:
    """Whether this world's db_code_verifier runs in legacy-fields mode.

    Legacy is the default (``True``). Opting into the new canonical-library
    mode is an active choice — the editor checkbox defaults to checked and
    must be explicitly turned OFF (persisting ``False``). Absent or ``null`` →
    legacy, so existing worlds are never affected and need no migration. The
    editor (box checked by default, coalescing ``null`` via ``?? true``) and
    grading/codegen agree: only an explicit ``False`` is new mode.
    """
    value = eval_config_values.get("allow_legacy_fields")
    return value is None or bool(value)


def resolve_db_allowed_imports(eval_config_values: dict[str, Any]) -> list[str]:
    """Resolve the AST-gate import allowlist for a db_code_verifier run.

    New mode returns the canonical blessed set and ignores any stored
    ``allowed_imports`` (libraries are no longer per-world configurable).
    Legacy mode unions a stored ``allowed_imports`` list (kept first, deduped)
    with the blessed set — matching the export/QC resolver so a stale
    per-world list can't fail as infra here what grades fine in a delivered
    container. The security floor is the AST gate's banned sets, not this list.
    """
    if not is_legacy_fields_mode(eval_config_values):
        return list(DB_CODE_VERIFIER_BLESSED_IMPORTS)

    configured = eval_config_values.get("allowed_imports")
    explicit = [str(x) for x in configured] if isinstance(configured, list) else []
    return list(dict.fromkeys([*explicit, *DB_CODE_VERIFIER_BLESSED_IMPORTS]))
