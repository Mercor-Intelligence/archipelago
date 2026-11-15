"""Replay — call the SUT and normalize its response (spec §1/§6.1).

Replay is binding-aware but comparator-agnostic: it produces a normalized result
that :mod:`parity_test.compare` diffs against a snapshot the same way regardless
of binding.

- **REST** (:func:`replay_rest`): issue the request against a client and return
  status + headers + parsed body. The client is anything with a
  ``request(method, url, *, params=, json=, headers=)`` method returning an
  httpx/Starlette-style response (``status_code``, ``headers``, ``json()`` /
  ``content``) — the clone ``TestClient`` in replay, the live reference in
  capture. The base URL is the client's concern; ``url`` here is relative
  (built from the OAS operation).
- **MCP** (:func:`replay_mcp`): POST a JSON-RPC ``tools/call`` **through the
  whole middleware stack** (DECIDED, spec §6.1), decode plain-JSON *and* SSE,
  then :func:`parse_mcp_tool_content` normalizes the content so an MCP result and
  the canonical REST body compare alike. FastMCP's streamable-HTTP transport is
  session-oriented, so the first call performs the ``initialize`` handshake and
  caches the ``Mcp-Session-Id`` on the client (harmless if the server is
  stateless — no session id comes back and we simply omit the header).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReplayResult:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    binding: str = "rest"

    # Binding-agnostic accessors (spec §6.1) — let a single test body read a REST
    # or an MCP result identically, so ``api.invoke(binding, ...)`` can be
    # parametrized over both bindings when the schema is the same. ``failed`` is
    # the outcome axis (REST status ≥ 400 ↔ MCP ``isError``); ``data`` is the
    # canonical body (REST ``body`` ↔ MCP ``content``) — the same sanitized shape
    # on both sides.
    @property
    def failed(self) -> bool:
        return self.status >= 400

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def data(self) -> Any:
        return self.body


@dataclass
class McpReplayResult:
    is_error: bool
    content: Any = None
    structured_content: Any = None
    raw: Any = None
    binding: str = "mcp"

    @property
    def failed(self) -> bool:
        return bool(self.is_error)

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def data(self) -> Any:
        return self.content


def _parse_body(resp: Any) -> Any:
    ctype = ""
    headers = getattr(resp, "headers", {}) or {}
    try:
        ctype = headers.get("content-type", "") if hasattr(headers, "get") else ""
    except Exception:
        ctype = ""
    if "json" in ctype.lower():
        try:
            return resp.json()
        except Exception:
            pass
    # Fall back: try json, then text, then None.
    try:
        return resp.json()
    except Exception:
        text = getattr(resp, "text", None)
        return text if text not in (None, "") else None


def replay_rest(
    client: Any,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any = None,
    headers: dict[str, str] | None = None,
) -> ReplayResult:
    """Issue a REST call and return a normalized result."""
    resp = client.request(method.upper(), url, params=params, json=json, headers=headers)
    return ReplayResult(
        status=getattr(resp, "status_code", 0),
        headers={k.lower(): v for k, v in dict(getattr(resp, "headers", {}) or {}).items()},
        body=_parse_body(resp),
    )


# --- MCP replay --------------------------------------------------------------
#
# POST a JSON-RPC ``tools/call`` to the MCP endpoint the same client REST replay
# uses (for a FastMCP app that is ``mcp.http_app(path=..., transport=
# "streamable-http")``). Accept a plain-JSON or an SSE reply, pull
# ``result.content`` / ``result.isError`` out of the envelope, and normalize the
# content the same way capture did — otherwise the body diff would compare
# parsed-vs-unparsed and always "fail". Semantics mirror the studio's
# ``executeMcp`` / ``parseMcpToolContent``.

_MCP_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}
_SESSION_ATTR = "_parity_mcp_session_id"


def _ensure_mcp_session(client: Any, mcp_path: str) -> str:
    """Perform the MCP ``initialize`` handshake once per client and cache the
    ``Mcp-Session-Id`` (empty string when the server is stateless)."""
    cached = getattr(client, _SESSION_ATTR, None)
    if cached is not None:
        return cached

    init = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "parity-test", "version": "0"},
        },
    }
    resp = client.request("POST", mcp_path, headers=_MCP_HEADERS, json=init)
    session_id = ""
    try:
        session_id = resp.headers.get("mcp-session-id", "") or ""
    except Exception:
        session_id = ""

    # Complete the handshake: notify the server we're initialized.
    note_headers = dict(_MCP_HEADERS)
    if session_id:
        note_headers["mcp-session-id"] = session_id
    try:
        client.request(
            "POST",
            mcp_path,
            headers=note_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
    except Exception:
        pass

    try:
        setattr(client, _SESSION_ATTR, session_id)
    except Exception:
        pass
    return session_id


def replay_mcp(
    client: Any,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    mcp_path: str = "/",
) -> McpReplayResult:
    """Call an MCP tool through the full middleware stack (spec §6.1).

    Returns a :class:`McpReplayResult` whose ``content`` is the *normalized* tool
    output (embedded JSON extracted, stringified JSON expanded) so it diffs
    directly against the canonical snapshot body.
    """
    session_id = _ensure_mcp_session(client, mcp_path)
    headers = dict(_MCP_HEADERS)
    if session_id:
        headers["mcp-session-id"] = session_id

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments or {}},
    }
    response = client.request("POST", mcp_path, headers=headers, json=payload)

    raw = _decode_mcp_envelope(response)
    result = raw.get("result") if isinstance(raw, dict) else None
    if isinstance(result, dict):
        raw_content = result.get("content")
        structured = result.get("structuredContent")
        is_error = bool(result.get("isError"))
    else:
        raw_content = raw
        structured = None
        is_error = False

    return McpReplayResult(
        is_error=is_error,
        content=parse_mcp_tool_content(raw_content),
        structured_content=structured,
        raw=raw,
    )


def _decode_mcp_envelope(response: Any) -> Any:
    """Pull the JSON-RPC envelope out of an MCP HTTP response — plain JSON or SSE
    (streamable-HTTP servers reply with ``text/event-stream``)."""
    text = getattr(response, "text", "") or ""
    try:
        ct = str(response.headers.get("content-type", "")).lower()
    except Exception:
        ct = ""
    if "text/event-stream" in ct:
        envelope = _extract_sse_jsonrpc(text)
        return envelope if envelope is not None else {"parseError": text}
    try:
        return response.json()
    except (ValueError, TypeError):
        pass
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return {"parseError": text}


def _extract_sse_jsonrpc(text: str) -> Any:
    """Pull the first JSON-RPC envelope (one carrying ``result`` or ``error``)
    out of an SSE stream — streamable-HTTP MCP servers reply as ``data:`` lines."""
    for event in re.split(r"\r?\n\r?\n", text):
        for line in re.split(r"\r?\n", event):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            try:
                parsed = json.loads(payload)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
                return parsed
    return None


# --- MCP content normalization -----------------------------------------------
#
# Ported (semantics-wise) from the studio's ``parseMcpToolContent`` so a replayed
# tool result normalizes to the same shape capture stored — otherwise the body
# comparison would diff parsed-vs-unparsed and always "fail".


def parse_mcp_tool_content(content: Any) -> Any:
    """Turn an MCP tool-call ``content`` payload into clean, referenceable JSON.

    MCP results are usually ``[{"type": "text", "text": "<json>"}]``, but the
    text may carry a human prefix, extra noise blocks, and nested stringified
    JSON. Extract the embedded JSON, keep only the data block(s), and expand
    nested JSON strings.
    """
    if not isinstance(content, list):
        return content
    texts = [
        b["text"]
        for b in content
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]
    if not texts:
        return content

    data_blocks: list[Any] = []
    plain: list[str] = []
    for t in texts:
        parsed = _extract_embedded_json(t)
        if isinstance(parsed, (dict, list)):
            data_blocks.append(_deep_parse_json_strings(parsed))
        else:
            plain.append(t)

    if len(data_blocks) == 1:
        return data_blocks[0]
    if len(data_blocks) > 1:
        return data_blocks
    return plain[0] if len(plain) == 1 else plain


def _extract_embedded_json(text: str) -> Any:
    """Extract a JSON object/array embedded in a text block, even when prefixed
    with human text like ``"Retrieved successfully.\\n{...}"``. Returns the
    parsed value, or ``None`` when no balanced JSON object/array is found."""
    trimmed = text.strip()
    try:
        return json.loads(trimmed)
    except (ValueError, TypeError):
        pass

    start = -1
    for i, ch in enumerate(trimmed):
        if ch in "{[":
            start = i
            break
    if start < 0:
        return None
    open_ch = trimmed[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(trimmed)):
        c = trimmed[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(trimmed[start : i + 1])
                except (ValueError, TypeError):
                    return None
    return None


def _deep_parse_json_strings(value: Any, depth: int = 0) -> Any:
    """Recursively expand string values that are themselves stringified JSON
    (e.g. a nested ``rawResponse: "{...}"``) so the parsed body is fully
    structured rather than holding escaped-JSON blobs."""
    if depth > 6:
        return value
    if isinstance(value, str):
        t = value.strip()
        looks_json = (t.startswith("{") and t.endswith("}")) or (
            t.startswith("[") and t.endswith("]")
        )
        if looks_json and len(t) <= 2_000_000:
            try:
                parsed = json.loads(t)
            except (ValueError, TypeError):
                return value
            if isinstance(parsed, (dict, list)):
                return _deep_parse_json_strings(parsed, depth + 1)
        return value
    if isinstance(value, list):
        return [_deep_parse_json_strings(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {k: _deep_parse_json_strings(v, depth + 1) for k, v in value.items()}
    return value
