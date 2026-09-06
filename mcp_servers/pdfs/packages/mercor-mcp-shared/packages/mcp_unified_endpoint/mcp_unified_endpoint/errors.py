"""Global exception → status code registry with per-endpoint overrides.

Servers register the default mappings once at startup (e.g.
``register_default_errors({InvalidModule: 400, NotFound: 404})``). Each
``@endpoint`` may pass an ``on_error`` dict that wins over the global
defaults for that one route.

Two override shapes are supported per exception class:

* ``int`` — a custom status code. The envelope body is still built by
  the registered :data:`EnvelopeBuilder` (Zoho V8 shape by default), so
  this is the right form when only the HTTP status differs from the
  global default.
* ``Callable[[exc, request_kwargs], dict]`` — an envelope builder. The
  callable receives the raised exception and the kwargs the handler
  parsed for this request (so it can drop request-scoped values into
  ``details`` like ``{"id": kwargs["note_id"]}``). The status defaults
  to 400; wrap the callable in ``(builder, status)`` to override.

The decorator's REST handler catches any registered exception, picks the
envelope + status, and returns ``JSONResponse(envelope,
status_code=...)``. Unmapped exceptions propagate (Starlette turns them
into 500s the same way it always has).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

# Module-level state: a dict ``{ExceptionClass: status_code}`` looked up by
# walking the exception's MRO. Sites set this once at startup.
_DEFAULT_ERRORS: dict[type[BaseException], int] = {}

# Envelope shape produced by the **global** default: ``(exc, status_code)
# -> dict``. Default is the Zoho V8 shape; servers can swap by passing
# ``envelope_builder=...`` to ``register_default_errors``.
EnvelopeBuilder = Callable[[BaseException, int], dict[str, Any]]

# Per-endpoint envelope builder: ``(exc, request_kwargs) -> dict``. The
# kwargs map gives the builder access to whichever path / query / body
# values the handler parsed, so ``details`` can reference them by name.
ErrorBuilder = Callable[[BaseException, Mapping[str, Any]], dict[str, Any]]

# What the ``on_error`` mapping may carry per exception class.
ErrorSpec = int | ErrorBuilder | tuple[ErrorBuilder, int]


def _default_zoho_envelope(exc: BaseException, status_code: int) -> dict[str, Any]:
    """Default Zoho-V8-shape envelope.

    ``{"code": …, "message": …, "details": {…}, "status": "error"}``. Code
    is the exception's class name in upper-snake form; message is
    ``str(exc)``. Details come from a ``details`` attribute when present.
    """
    code = (
        "".join(("_" + c) if c.isupper() and i else c for i, c in enumerate(type(exc).__name__))
        .upper()
        .lstrip("_")
    )
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        details = {}
    return {
        "code": code,
        "message": str(exc) or type(exc).__name__,
        "details": details,
        "status": "error",
    }


_ENVELOPE_BUILDER: EnvelopeBuilder = _default_zoho_envelope


def register_default_errors(
    mapping: Mapping[type[BaseException], int],
    *,
    envelope_builder: EnvelopeBuilder | None = None,
    replace: bool = False,
) -> None:
    """Register the global default exception → status code mapping.

    Call once at server startup.

    Args:
        mapping: ``{ExceptionClass: status_code}``. Order is irrelevant;
            lookup uses the exception's MRO and picks the most specific
            class that has been registered.
        envelope_builder: Optional override for the envelope shape. Default
            is the Zoho V8 shape (see :func:`_default_zoho_envelope`).
        replace: When True, clears any previously-registered defaults
            before applying ``mapping``. Default False (mappings merge).
    """
    global _ENVELOPE_BUILDER
    if replace:
        _DEFAULT_ERRORS.clear()
    _DEFAULT_ERRORS.update(mapping)
    if envelope_builder is not None:
        _ENVELOPE_BUILDER = envelope_builder


def resolve_status(
    exc: BaseException,
    *,
    overrides: Mapping[type[BaseException], ErrorSpec] | None = None,
) -> int | None:
    """Return the status code for ``exc``, or ``None`` if not registered.

    Kept for backward compatibility — the synthesised REST handler now
    calls :func:`resolve_error` so it gets both the envelope and the
    status in one walk. This helper still works when the per-endpoint
    override is the simple ``int`` form; builder-shaped overrides return
    their declared status (or 400 when none is given).
    """
    for cls in type(exc).__mro__:
        if overrides is not None and cls in overrides:
            spec = overrides[cls]
            return _status_from_spec(spec)
        if cls in _DEFAULT_ERRORS:
            return _DEFAULT_ERRORS[cls]
    return None


def resolve_error(
    exc: BaseException,
    request_kwargs: Mapping[str, Any],
    *,
    overrides: Mapping[type[BaseException], ErrorSpec] | None = None,
) -> tuple[dict[str, Any], int] | None:
    """Return ``(envelope_body, status_code)`` for ``exc``, or ``None``.

    Walks the exception's MRO and picks the most-specific class that has
    either a per-endpoint override or a global default. Override shapes:

    * ``int``                  → default envelope builder, custom status.
    * ``ErrorBuilder``         → builder(exc, kwargs); status defaults to 400.
    * ``(ErrorBuilder, int)``  → builder(exc, kwargs); custom status.

    The global default registry only carries ``int`` (status only); the
    envelope falls back to :func:`build_envelope`.
    """
    for cls in type(exc).__mro__:
        if overrides is not None and cls in overrides:
            return _resolve_spec(exc, request_kwargs, overrides[cls])
        if cls in _DEFAULT_ERRORS:
            status = _DEFAULT_ERRORS[cls]
            return build_envelope(exc, status), status
    return None


def _resolve_spec(
    exc: BaseException,
    request_kwargs: Mapping[str, Any],
    spec: ErrorSpec,
) -> tuple[dict[str, Any], int]:
    """Materialise an :data:`ErrorSpec` into ``(envelope, status_code)``."""
    if isinstance(spec, int):
        return build_envelope(exc, spec), spec
    if isinstance(spec, tuple):
        builder, status = spec
        return builder(exc, request_kwargs), status
    # callable
    return spec(exc, request_kwargs), 400


def _status_from_spec(spec: ErrorSpec) -> int:
    """Pull the status out of an :data:`ErrorSpec` without invoking the builder."""
    if isinstance(spec, int):
        return spec
    if isinstance(spec, tuple):
        return spec[1]
    return 400


def build_envelope(exc: BaseException, status_code: int) -> dict[str, Any]:
    """Format ``exc`` into a response body via the registered envelope builder."""
    return _ENVELOPE_BUILDER(exc, status_code)


def _reset_for_tests() -> None:
    """Clear all registered state. Test-only."""
    global _ENVELOPE_BUILDER
    _DEFAULT_ERRORS.clear()
    _ENVELOPE_BUILDER = _default_zoho_envelope


__all__ = [
    "EnvelopeBuilder",
    "ErrorBuilder",
    "ErrorSpec",
    "build_envelope",
    "register_default_errors",
    "resolve_error",
    "resolve_status",
]
