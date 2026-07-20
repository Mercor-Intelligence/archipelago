"""Static AST-level pre-flight check for user-authored verifier code.

SECURITY MODEL — read this before touching this file.

The AST gate is NOT the security boundary. The trust boundary is the subprocess
that runs in ``main.py``: dropped privileges (uid 65534), stripped env, kernel
resource limits (rlimit via preexec_fn). The subprocess is what actually
contains a malicious or buggy verifier.

The gate is a fail-fast filter:
  * Catches obvious LLM mistakes (forgetting to wrap in def check(ctx), calling
    open() instead of ctx.read_text, importing a banned module) at codegen
    time so authors get clean error messages instead of opaque subprocess
    failures.
  * Closes the most common known escape patterns (bare-name bans, dunder
    traversal, attribute-name bans) so a *casual* attacker has to try harder.

The gate CANNOT reliably stop a determined attacker. Python's name binding is
dynamic — every escape recipe ever published exploits some legitimate-looking
expression that resolves to a banned object at runtime. A few examples this
gate does NOT catch:

  * ``__builtins__.__dict__["e" + "x" + "ec"]``       (string-built names)
  * ``().__class__.__base__.__subclasses__()``        (type-traversal)
  * ``[c for c in type.__mro__(type) if "..."]``      (MRO walk)
  * Pickle-deserialization gadgets reachable via numpy / pandas attributes

If the gate is your only defense, you have no defense. The kernel layer is
what enforces; the gate is what produces useful authoring-time feedback.

TODO (post-PR-1): tighten the subprocess further with a seccomp-bpf filter
blocking ``socket(2)`` and a network namespace via ``unshare -n``. rlimits
alone do not prevent network egress.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from .config import MAX_CODE_LENGTH_CHARS, PERMANENTLY_BANNED_IMPORTS

# Builtins that have no legitimate use in a verifier. ctx.read_text / read_bytes
# replace open(); literal evaluation of strings is replaced by json.loads.
_BANNED_BUILTINS: frozenset[str] = frozenset(
    {
        "__import__",
        "exec",
        "eval",
        "compile",
        "open",
        "input",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
    }
)

# Dunder attributes that allow walking the object graph back to builtins.
# __name__/__doc__ are benign and explicitly allowed.
_BENIGN_DUNDERS: frozenset[str] = frozenset({"__name__", "__doc__"})

# Bare names that grant access to the builtin namespace without an import.
# ``__builtins__`` is auto-injected into every module; banning it stops
# patterns like ``b = __builtins__; o = b.open; o(path)`` that would otherwise
# bypass _BANNED_BUILTINS (the call site has func=Name('o'), not 'open').
_BANNED_NAMES: frozenset[str] = frozenset({"__builtins__", "builtins"})


@dataclass
class GateResult:
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


class _Visitor(ast.NodeVisitor):
    def __init__(self, allowed: frozenset[str]) -> None:
        self.allowed = allowed
        self.violations: list[str] = []
        self.has_check_function = False
        # Depth counts how deep we are inside function/class scopes. The
        # contract is "top-level def check(ctx)"; nested functions or methods
        # named "check" (e.g., class Foo: def check(self, data)) are user
        # business and must not trip the signature rule.
        self._scope_depth = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self._scope_depth == 0 and node.name == "check":
            self.has_check_function = True
            args = [a.arg for a in node.args.args]
            if args != ["ctx"]:
                self.violations.append(
                    f"check() must take exactly one positional arg named 'ctx' (got {args})"
                )
        self._scope_depth += 1
        self.generic_visit(node)
        self._scope_depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if self._scope_depth == 0 and node.name == "check":
            # Mark the function as present so check_code does not *also* emit
            # "code must define a top-level function: def check(ctx)" — the
            # function does exist, it's just the wrong flavor.
            self.has_check_function = True
            self.violations.append("check() must be synchronous, not async")
        self._scope_depth += 1
        self.generic_visit(node)
        self._scope_depth -= 1

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope_depth += 1
        self.generic_visit(node)
        self._scope_depth -= 1

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_module(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # node.level > 0 means relative: catches both `from . import X` (where
        # node.module is None) and `from .pkg import X` (where node.module is
        # set but the import is still relative and would fail at runtime).
        if node.level and node.level > 0:
            self.violations.append("relative imports are not allowed")
            return
        if node.module is None:
            self.violations.append("relative imports are not allowed")
            return
        self._check_module(node.module)

    def _check_module(self, dotted: str) -> None:
        top = dotted.split(".")[0]
        if top in PERMANENTLY_BANNED_IMPORTS:
            self.violations.append(f"permanently banned import: {dotted}")
            return
        # Match either the exact dotted form or the top-level module.
        if dotted in self.allowed or top in self.allowed:
            return
        self.violations.append(f"import not in allowlist: {dotted}")

    def visit_Name(self, node: ast.Name) -> None:
        # Only flag reads (Load context). Stores are harmless (and useless)
        # shadows: ``for exec in []`` is silly but not dangerous; ``f = exec``
        # is a read of ``exec`` on the RHS (visited separately as Load) and
        # gets caught there.
        if isinstance(node.ctx, ast.Load):
            if node.id in _BANNED_NAMES:
                self.violations.append(f"forbidden name reference: {node.id}")
            elif node.id in _BANNED_BUILTINS:
                # Catches the alias attack: ``f = exec`` (RHS is a Load of
                # ``exec``), ``g = open`` (RHS Load), as well as direct calls
                # like ``exec(...)`` (the call resolves ``exec`` via a Load).
                # Once a banned builtin is referenced anywhere as a value, the
                # gate stops the verifier — even if the call site uses an
                # aliased name the gate can't follow.
                self.violations.append(f"forbidden builtin reference: {node.id}")
        self.generic_visit(node)

    # No visit_Call override is needed for banned builtins: the call's func
    # is an ast.Name in Load context, which visit_Name already rejects via
    # the _BANNED_BUILTINS check. Adding a redundant rule here would emit
    # two violations for one offense (one "call", one "reference").

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = node.attr
        if (
            name.startswith("__")
            and name.endswith("__")
            and name not in _BENIGN_DUNDERS
        ):
            self.violations.append(f"forbidden dunder access: {name}")
        elif name in _BANNED_BUILTINS:
            # Catches io.open, builtins.exec, getattr(x, 'open')(), and any
            # other attribute-chain access whose final attr is a banned builtin.
            self.violations.append(f"forbidden attribute access: {name}")
        self.generic_visit(node)


def check_code(
    code: str,
    allowed_imports: list[str] | None,
    max_length: int | None = None,
) -> GateResult:
    """Validate user-authored verifier code against the static gate.

    Returns a GateResult; callers should reject if ``result.ok`` is False.

    ``max_length`` overrides the default MAX_CODE_LENGTH_CHARS cap. Pass the
    world-level ``max_code_length_chars`` from EvalConfig so admin tuning
    actually affects grading.
    """
    result = GateResult()
    limit = max_length if max_length is not None else MAX_CODE_LENGTH_CHARS

    if not isinstance(code, str) or not code.strip():
        result.violations.append("code must be a non-empty string")
        return result

    if len(code) > limit:
        result.violations.append(f"code exceeds maximum length ({len(code)} > {limit})")
        return result

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        result.violations.append(f"syntax error: {exc.msg} (line {exc.lineno})")
        return result

    allowed = frozenset(allowed_imports or [])
    visitor = _Visitor(allowed)
    visitor.visit(tree)

    if not visitor.has_check_function:
        visitor.violations.append(
            "code must define a top-level function: def check(ctx)"
        )

    result.violations = visitor.violations
    return result
