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

# (receiver, attribute) pairs that share a name with a banned builtin but are
# stdlib calls with no path to the builtin of that name. The attribute rule below
# reads only ``node.attr``, so ``re.compile`` was rejected exactly like
# ``builtins.compile`` and every verifier using a regex failed the gate.
#
# Safe because the receiver must be a bare Name: ``builtins`` is in
# PERMANENTLY_BANNED_IMPORTS (so ``import builtins as re`` is refused by
# _check_module, which reads the real module name, not the alias) and both
# ``builtins`` and ``__builtins__`` are in _BANNED_NAMES, so the name ``re``
# cannot be bound to the builtin namespace. ``re.compile`` returns a Pattern,
# not a code object, and ``exec``/``eval`` remain banned as names AND as
# attributes, so nothing here gains an execution path.
_SAFE_ATTRIBUTE_PAIRS: frozenset[tuple[str, str]] = frozenset({("re", "compile")})


def _is_safe_attribute(node: ast.Attribute) -> bool:
    """Whether this attribute is an allowlisted stdlib call, not a banned builtin.

    Requires a BARE Name receiver on purpose. ``x.re.compile`` or
    ``getattr(m, "re").compile`` are not Name receivers, so they stay refused —
    the allowance never widens past the one shape it was measured on.
    """
    receiver = node.value
    return (
        isinstance(receiver, ast.Name)
        and (receiver.id, node.attr) in _SAFE_ATTRIBUTE_PAIRS
    )


def _is_literal_getattr(node: ast.Call) -> bool:
    """Whether this is ``getattr(obj, "<literal>"[, default])`` naming a safe attr.

    ``getattr`` is banned outright for ``getattr(x, "open")()``: the attribute is
    a string literal, so ``visit_Attribute`` never sees it. Testing that literal
    against the same banned sets closes exactly that hole, which leaves the shape
    verifiers actually use -- reading a field off the sanctioned ``ctx``. A
    non-literal name (``getattr(ctx, reader)``) is not decidable here and stays
    refused, the same discipline ``_is_safe_attribute`` applies to its receiver.
    """
    func = node.func
    if not (isinstance(func, ast.Name) and func.id == "getattr"):
        return False
    if len(node.args) not in (2, 3):
        return False
    attr = node.args[1]
    if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
        return False
    name = attr.value
    if name in _BANNED_BUILTINS or name in _BANNED_NAMES:
        return False
    return not (
        name.startswith("__") and name.endswith("__") and name not in _BENIGN_DUNDERS
    )


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
        # ``id()`` of every ``getattr`` Name that ``visit_Call`` cleared, read
        # back in ``visit_Name``. Keyed on the node rather than the name so one
        # safe call never exempts a bare ``getattr`` elsewhere in the file.
        self._safe_getattr_funcs: set[int] = set()

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
            elif (
                node.id in _BANNED_BUILTINS and id(node) not in self._safe_getattr_funcs
            ):
                # Catches the alias attack: ``f = exec`` (RHS is a Load of
                # ``exec``), ``g = open`` (RHS Load), as well as direct calls
                # like ``exec(...)`` (the call resolves ``exec`` via a Load).
                # Once a banned builtin is referenced anywhere as a value, the
                # gate stops the verifier — even if the call site uses an
                # aliased name the gate can't follow.
                self.violations.append(f"forbidden builtin reference: {node.id}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # Only ever REGISTERS an exemption -- visit_Name still owns the report, so
        # one offense yields one violation, not a "call" and a "reference" pair.
        # Runs before generic_visit so node.func is cleared before it is visited.
        if _is_literal_getattr(node):
            self._safe_getattr_funcs.add(id(node.func))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = node.attr
        if (
            name.startswith("__")
            and name.endswith("__")
            and name not in _BENIGN_DUNDERS
        ):
            self.violations.append(f"forbidden dunder access: {name}")
        elif name in _BANNED_BUILTINS and not _is_safe_attribute(node):
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
