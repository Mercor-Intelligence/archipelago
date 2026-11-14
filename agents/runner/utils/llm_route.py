"""The LLM route a harness calls through, in the shape each client type needs."""

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from types import EllipsisType
from typing import Any, Literal
from urllib.parse import urlsplit

from loguru import logger

from runner.utils.decorators import campaign_id_ctx, trajectory_batch_id_ctx
from runner.utils.settings import (
    DIRECT_PROXY_CLIENT_RESPONSE_BUFFER_S,
    GATEWAY_CLIENT_RESPONSE_BUFFER_S,
    Settings,
    gateway_routing_enabled_ctx,
    get_settings,
    priority_ctx,
    work_unit_ctx,
)

# Same strings grading reads; agents cannot import grading.
ROUTING_ENV = "LLM_GATEWAY_ROUTING_ENABLED"
PRIORITY_ENV = "LLM_GATEWAY_PRIORITY"
CAMPAIGN_ENV = "LLM_GATEWAY_CAMPAIGN_ID"
WORK_UNIT_ENV = "LLM_GATEWAY_WORK_UNIT"

_ANTHROPIC_PREFIXES = ("anthropic/", "anthropic_eap/")
_PRIVATE_TLDS = frozenset({"local", "internal", "localhost"})


@dataclass(frozen=True)
class LlmRoute:
    via_gateway: bool
    base_url: str
    api_key: str
    headers: dict[str, str]

    def call_headers(
        self, timeout_s: float | None, *, stream_relay: bool = False
    ) -> dict[str, str]:
        """Per-call gateway headers; `{}` on the LiteLLM route."""
        if not self.via_gateway:
            return {}
        hdrs = dict(self.headers)
        if timeout_s is not None and timeout_s > 0:
            hdrs["X-Gateway-Timeout-S"] = str(int(timeout_s))
        if stream_relay:
            hdrs["X-Gateway-Stream-Mode"] = "relay"
        return hdrs

    def client_timeout(self, timeout_s: float) -> float:
        """The client read timeout that outwaits the route's own deadline."""
        if self.via_gateway:
            return timeout_s + GATEWAY_CLIENT_RESPONSE_BUFFER_S
        return timeout_s + DIRECT_PROXY_CLIENT_RESPONSE_BUFFER_S

    def anthropic_client_kwargs(self, *, litellm_path: str = "") -> dict[str, Any]:
        """Anthropic SDK client kwargs; `litellm_path` applies only on the LiteLLM route."""
        if self.via_gateway:
            # The SDK appends /v1/messages itself.
            return {
                "base_url": self.base_url.removesuffix("/v1"),
                "api_key": self.api_key,
                "default_headers": dict(self.headers),
            }
        return {"base_url": self.base_url + litellm_path, "api_key": self.api_key}

    def anthropic_model(self, model: str) -> str:
        """Messages API model name: prefixed for the gateway's router, bare for the passthrough."""
        if self.via_gateway:
            return model if "/" in model else f"anthropic/{model}"
        for prefix in _ANTHROPIC_PREFIXES:
            if model.startswith(prefix):
                return model.removeprefix(prefix)
        return model

    def openai_client_kwargs(self) -> dict[str, Any]:
        """OpenAI SDK client kwargs."""
        kwargs: dict[str, Any] = {"base_url": self.base_url, "api_key": self.api_key}
        if self.via_gateway:
            kwargs["default_headers"] = dict(self.headers)
        return kwargs

    def genai_client_kwargs(self) -> dict[str, Any]:
        """google-genai client kwargs."""
        http_options: dict[str, Any] = {"base_url": self.base_url + "/gemini"}
        if self.via_gateway:
            http_options["headers"] = dict(self.headers)
        return {"api_key": self.api_key, "http_options": http_options}

    def cli_env(
        self, client: Literal["anthropic", "openai"], *, timeout_s: float | None = None
    ) -> dict[str, str]:
        """Env for a CLI child process; OpenAI-style CLIs take headers from their own config."""
        if client == "openai":
            return {"OPENAI_BASE_URL": self.base_url, "OPENAI_API_KEY": self.api_key}
        if not self.via_gateway:
            return {
                "ANTHROPIC_BASE_URL": self.base_url,
                "ANTHROPIC_API_KEY": self.api_key,
            }
        headers = self.call_headers(timeout_s, stream_relay=True)
        return {
            "ANTHROPIC_BASE_URL": self.base_url.removesuffix("/v1"),
            "ANTHROPIC_API_KEY": self.api_key,
            "ANTHROPIC_CUSTOM_HEADERS": "\n".join(
                f"{k}: {v}" for k, v in headers.items()
            ),
        }


def resolve_llm_route(
    *,
    workload: str | None,
    campaign_id: str | None | EllipsisType = ...,
    priority: int | None = None,
    settings: Settings | None = None,
) -> LlmRoute | None:
    """The route `Settings.apply_llm_target` would pick; None when neither gateway nor LiteLLM is set.

    `campaign_id` defaults to `campaign_id_ctx`.
    """
    settings = settings or get_settings()
    if campaign_id is ...:
        campaign_id = campaign_id_ctx.get()
    if (
        settings.is_gateway_routed()
        and settings.LLM_GATEWAY_API_BASE
        and settings.LLM_GATEWAY_API_KEY
    ):
        return LlmRoute(
            via_gateway=True,
            base_url=settings.LLM_GATEWAY_API_BASE.rstrip("/"),
            api_key=settings.LLM_GATEWAY_API_KEY,
            headers=settings.gateway_headers(
                campaign_id=campaign_id, workload=workload, priority=priority
            ),
        )
    if settings.LITELLM_PROXY_API_BASE and settings.LITELLM_PROXY_API_KEY:
        return LlmRoute(
            via_gateway=False,
            base_url=settings.LITELLM_PROXY_API_BASE.rstrip("/"),
            api_key=settings.LITELLM_PROXY_API_KEY,
            headers={},
        )
    return None


def trusted_llm_hosts(settings: Settings | None = None) -> tuple[str, ...]:
    """Hostnames of the configured LiteLLM proxy and LLM Gateway, for sandbox egress.

    Raises when a configured base is not a plain HTTPS URL on the default port.
    """
    settings = settings or get_settings()
    hosts: list[str] = []
    for name, base in (
        ("LITELLM_PROXY_API_BASE", settings.LITELLM_PROXY_API_BASE),
        ("LLM_GATEWAY_API_BASE", settings.LLM_GATEWAY_API_BASE),
    ):
        if not base or not base.strip():
            continue
        parsed = urlsplit(base.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise RuntimeError(f"{name} must be an HTTPS URL")
        if parsed.username or parsed.password:
            raise RuntimeError(f"{name} must not contain credentials")
        try:
            port = parsed.port
        except ValueError as exc:
            raise RuntimeError(f"{name} has an invalid port") from exc
        if port not in (None, 443):
            raise RuntimeError(f"{name} must use the HTTPS default port")
        host = parsed.hostname.lower()
        if _is_private_host(host):
            raise RuntimeError(f"{name} must be a public hostname")
        hosts.append(host)
    return tuple(hosts)


def _is_private_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return True
    labels = host.split(".")
    return len(labels) < 2 or labels[-1] in _PRIVATE_TLDS


def trajectory_workload() -> str:
    """The gateway workload of this trajectory's own calls."""
    return "trajectory_batch" if trajectory_batch_id_ctx.get() else "trajectory_single"


def mcp_route_fields(
    *,
    app: str,
    declares_llm_gateway: bool,
    accepts_extra_headers: bool,
    extra_headers: Mapping[str, str] | None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run-request fields that route a CLI app's model calls; `{}` with no route or an old app build."""
    route = resolve_llm_route(workload=trajectory_workload(), settings=settings)
    if route is None:
        return {}
    if not declares_llm_gateway:
        logger.info(
            f"{app}: this app build does not declare llm_gateway, so Studio cannot "
            "route its model calls; re-pin the platform to a build that declares it"
        )
        return {}
    if not route.via_gateway:
        # The Codex and OpenCode apps read a missing field as "use the gateway".
        return {"llm_gateway": False}
    fields: dict[str, Any] = {"llm_gateway": True}
    if accepts_extra_headers:
        fields["extra_headers"] = {**route.headers, **(extra_headers or {})}
    return fields


def route_env() -> dict[str, str]:
    """This run's routing context vars as env, for `adopt_route_env` in a child process.

    Carries no credential: children get the gateway pair the way they do now.
    """
    env: dict[str, str] = {}
    if gateway_routing_enabled_ctx.get() is True:
        env[ROUTING_ENV] = "true"
    if (priority := priority_ctx.get()) is not None:
        env[PRIORITY_ENV] = str(priority)
    if campaign_id := campaign_id_ctx.get():
        env[CAMPAIGN_ENV] = campaign_id
    if work_unit := work_unit_ctx.get():
        env[WORK_UNIT_ENV] = work_unit
    return env


def adopt_route_env(environ: Mapping[str, str]) -> None:
    """Set the routing context vars from `route_env` output; a malformed value changes nothing."""
    if environ.get(ROUTING_ENV, "").strip().lower() == "true":
        gateway_routing_enabled_ctx.set(True)
    raw_priority = environ.get(PRIORITY_ENV, "").strip()
    if raw_priority.isascii() and raw_priority.isdigit() and int(raw_priority) <= 5:
        priority_ctx.set(int(raw_priority))
    if campaign_id := environ.get(CAMPAIGN_ENV, "").strip():
        campaign_id_ctx.set(campaign_id)
    if work_unit := environ.get(WORK_UNIT_ENV, "").strip():
        work_unit_ctx.set(work_unit)
