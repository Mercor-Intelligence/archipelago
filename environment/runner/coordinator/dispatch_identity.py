"""One-time nonces that let the Coordinator name the actor on its own tool calls.

`_meta` is forwarded verbatim to the backend MCP server, so the gateway redeems the
nonce before proxying the call: a backend can only ever replay a spent one.
"""

import secrets


class DispatchClaim:
    """What a redeemed nonce says about the call that carried it."""

    __slots__ = ("actor_id", "origin")

    def __init__(self, actor_id: str, origin: str) -> None:
        self.actor_id = actor_id
        self.origin = origin


_PENDING: dict[str, DispatchClaim] = {}


def issue_dispatch_nonce(actor_id: str, origin: str) -> str:
    """Mint a nonce that names `actor_id` for exactly one tool call."""
    nonce = secrets.token_urlsafe(32)
    _PENDING[nonce] = DispatchClaim(actor_id, origin)
    return nonce


def consume_dispatch_nonce(nonce: object) -> DispatchClaim | None:
    """Redeem a nonce, returning its claim the first time only."""
    if not isinstance(nonce, str):
        return None
    return _PENDING.pop(nonce, None)


def discard_dispatch_nonce(nonce: str) -> None:
    """Drop a nonce the gateway never saw, so a failed dispatch cannot accumulate."""
    _ = _PENDING.pop(nonce, None)
