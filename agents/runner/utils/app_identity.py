"""Mounted apps that refused the run's own actor on every call they served.

The populate-time sibling of this check lives in
``environment/runner/data/populate/main.py`` and only sees an app whose SEED
HOOK refused the actor. An app can seed perfectly and still reject the actor at
every tool call — Slack builds its user table from the seed and then refuses any
row it marks deactivated — and that shape reaches the trajectory with a clean
populate, ~15 identical 401s, and `trajectory_status: completed`. This module
reads the finished transcript so both shapes land on the same
``unavailable_apps`` key.

Three gates keep it narrow, because the cost of a false positive is telling a
reader an app was unusable when the agent simply mis-called it:

  * **The line names this run's actor.** A cause marker alone is ordinary
    English an app can emit about somebody else — an integration's service
    account, a user the agent looked up. Only the pinned address makes it "the
    app refused the person this trajectory runs as".
  * **The result is a failure.** A working user-search legitimately answers
    "user not found: <address>", and searching for one's own address is a
    thing an agent does; without this gate that success reads as a refusal.
  * **Every call the app served failed that way.** One rejected call among
    successes is a call the agent got wrong, not an app that refused the actor.

The status and the cause are matched against the WHOLE result rather than one
line, which is the one place this is looser than the populate check: the apps
that produce this render them on separate lines of one tool result
(``execution_failed: not_authed`` / ``No user found with email: …``).

ATTRIBUTION IS BY ``{server}_`` PREFIX, which is what fastmcp's multi-server
gateway serves. A world with exactly ONE mcp app gets unprefixed tool names, so
nothing is attributed and nothing is reported — a miss, never a false positive,
and the same direction the populate check errs in.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from runner.agents.models import (
    get_msg_attr,
    get_msg_content,
    get_msg_role,
    materialize_msg_content,
    set_msg_content,
)

#: Mirror of ``_IDENTITY_CAUSE_MARKERS`` in
#: ``environment/runner/data/populate/main.py``. The two packages deploy
#: separately and share no code — the same reason ``UnavailableApp`` is
#: declared twice — so the wording is pinned by test, not by import.
IDENTITY_CAUSE_MARKERS = (
    "no user found",
    "no such user",
    "user not found",
    "unknown user",
)

#: What makes a result a REFUSAL rather than a lookup that found nobody. Wider
#: than the populate check's ``_IDENTITY_STATUS_MARKERS`` because the transcript
#: carries the gateway's error envelope, which never says "401" at all, where a
#: hook carries the raw HTTP failure.
IDENTITY_FAILURE_MARKERS = (
    "execution_failed",
    "not_authed",
    "unauthenticated",
    "unauthorized",
    "authentication",
    "401",
    "403",
)

#: An address is matched at ADDRESS BOUNDARIES, never as a bare substring:
#: actor ``dan@corp.com`` must not be found inside ``jordan@corp.com``, which is
#: an ordinary failed lookup of somebody else. Mirrored in the populate module,
#: pinned by test for the same reason the cause markers are.
ACTOR_BOUNDARY_BEFORE = r"(?<![A-Za-z0-9._%+-])"
ACTOR_BOUNDARY_AFTER = r"(?![A-Za-z0-9-]|\.[A-Za-z0-9])"

#: The address Studio pins per task; every app in the sandbox reads this name.
ACTOR_EMAIL_VAR = "EXPERT_MODAL_ACTOR_EMAIL"

#: How the sandbox is HANDED that address, globally and per service. Mirrors
#: ``STUDIO_ENV_CARRIER_PREFIX`` / ``service_carrier_name`` in the server's
#: ``packages/islands/shared/studio_env.py``.
_CARRIER_PREFIX = "MERCOR_STUDIO_ENV__"
_CARRIER_SERVICE_SEP = "__"

#: Keep an operator-facing reason short enough to sit on a result payload.
_MAX_REASON_CHARS = 500

_NON_IDENT = re.compile(r"[^a-z0-9]+")


def fold_service_name(service_name: str) -> str:
    """The server's ``normalize_service_name`` fold, which keys both the mcp
    configs and the per-service carrier."""
    return _NON_IDENT.sub("_", service_name.lower()).strip("_")


def actor_email_for_app(app_name: str, studio_env: Mapping[str, str]) -> str:
    """The address THIS app was handed: its own carrier, else the global one.

    Same precedence as the sandbox's ``studio_env`` shell function, so a persona
    that overrides one app is judged against the address that app actually got.
    """
    folded = fold_service_name(app_name)
    per_app = studio_env.get(
        f"{_CARRIER_PREFIX}{folded.upper()}{_CARRIER_SERVICE_SEP}{ACTOR_EMAIL_VAR}", ""
    )
    return (
        per_app or studio_env.get(f"{_CARRIER_PREFIX}{ACTOR_EMAIL_VAR}", "")
    ).strip()


def actor_pattern(actor_email: str) -> re.Pattern[str]:
    """The address, matched only where an address ends rather than as a substring."""
    return re.compile(
        ACTOR_BOUNDARY_BEFORE + re.escape(actor_email) + ACTOR_BOUNDARY_AFTER,
        re.IGNORECASE,
    )


def _tool_result_text(message: Any) -> str:
    """One tool result as plain text, whether it arrived as a string or blocks.

    Stores materialized blocks back on the message: pydantic validates a
    multi-block tool ``content`` into a ONE-SHOT ``ValidatorIterator``, so
    reading it without writing it back leaves the message serializing as
    ``content: []``.
    """
    content = get_msg_content(message)
    materialized = materialize_msg_content(content)
    if materialized is not content:
        set_msg_content(message, materialized)
    if isinstance(materialized, str):
        return materialized
    if isinstance(materialized, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in materialized
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _tool_call_names(messages: Iterable[Any]) -> dict[str, str]:
    """``tool_call_id`` -> tool name, read off the assistant turns that issued them.

    The tool message's own ``name`` is the usual source but is optional in the
    Chat Completions schema and some providers drop it; the call that produced
    the result always names the function.
    """
    names: dict[str, str] = {}
    for message in messages:
        for call in get_msg_attr(message, "tool_calls") or []:
            call_id = (
                call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
            )
            function = (
                call.get("function")
                if isinstance(call, dict)
                else getattr(call, "function", None)
            )
            name = (
                function.get("name")
                if isinstance(function, dict)
                else getattr(function, "name", None)
            )
            if isinstance(call_id, str) and isinstance(name, str):
                names[call_id] = name
    return names


def _owning_app(tool_name: str, folded_apps: Mapping[str, str]) -> str | None:
    """The mounted app whose ``{server}_`` prefix this tool name carries.

    Longest prefix wins, so ``word`` never claims ``word_v2``'s tools.
    """
    lowered = tool_name.lower()
    best: str | None = None
    for folded in folded_apps:
        if lowered.startswith(f"{folded}_") and (
            best is None or len(folded) > len(best)
        ):
            best = folded
    return folded_apps[best] if best is not None else None


def _rejection_line(text: str, actor: re.Pattern[str]) -> str | None:
    """The line proving this app refused THIS actor, or None."""
    lowered_text = text.lower()
    if not any(marker in lowered_text for marker in IDENTITY_FAILURE_MARKERS):
        return None
    for line in text.splitlines():
        lowered = line.lower()
        if actor.search(line) and any(
            marker in lowered for marker in IDENTITY_CAUSE_MARKERS
        ):
            return line.strip()[:_MAX_REASON_CHARS]
    return None


def apps_that_rejected_the_actor(
    *,
    messages: Iterable[Any],
    app_names: Iterable[str],
    studio_env: Mapping[str, str],
) -> list[dict[str, str]]:
    """Apps every one of whose served tool calls refused the run's pinned actor.

    Returns ``UnavailableApp``-shaped dicts, ready to merge onto
    ``trajectory_output.unavailable_apps``. Empty whenever the run carried no
    actor, no mcp apps, or any call to an app succeeded. Reading a tool result
    materializes its content in place; see ``_tool_result_text``.
    """
    folded_apps = {
        folded: name for name in app_names if (folded := fold_service_name(name))
    }
    if not folded_apps:
        return []

    materialized = list(messages)
    call_names = _tool_call_names(materialized)
    served: dict[str, int] = {}
    refused: dict[str, int] = {}
    lines: dict[str, str] = {}
    patterns: dict[str, re.Pattern[str]] = {}

    for message in materialized:
        if get_msg_role(message) != "tool":
            continue
        call_id = get_msg_attr(message, "tool_call_id")
        tool_name = get_msg_attr(message, "name") or call_names.get(call_id or "", "")
        if not isinstance(tool_name, str) or not tool_name:
            continue
        app = _owning_app(tool_name, folded_apps)
        if app is None:
            continue
        served[app] = served.get(app, 0) + 1
        actor_email = actor_email_for_app(app, studio_env)
        if not actor_email:
            continue
        if actor_email not in patterns:
            patterns[actor_email] = actor_pattern(actor_email)
        line = _rejection_line(_tool_result_text(message), patterns[actor_email])
        if line is not None:
            refused[app] = refused.get(app, 0) + 1
            lines.setdefault(app, line)

    return [
        {
            "name": app,
            "reason": (
                f"Every tool call this run made to {app} ({count}) was refused: "
                f"{lines[app]}"
            )[:_MAX_REASON_CHARS],
        }
        for app, count in sorted(served.items())
        if count and refused.get(app) == count
    ]
