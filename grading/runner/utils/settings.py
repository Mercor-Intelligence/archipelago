from contextvars import ContextVar
from enum import Enum
from functools import cache
from typing import Any

from loguru import logger
from pydantic_settings import BaseSettings, SettingsConfigDict

# Backend-resolved LLM Gateway routing decision for this grading run's
# judge-model + workload tuple. Set at the worker entrypoint from the
# grading-config response. Read by `Settings.is_gateway_routed` so the
# worker doesn't need PostHog connectivity. Defaults to None which the
# gate treats as False (fail-closed) — keeps traffic on LiteLLM when an
# older server omits the field. Lives in this module (not decorators.py)
# because decorators imports from metrics which imports from settings —
# colocating with the consumer avoids that cycle.
gateway_routing_enabled_ctx: ContextVar[bool | None] = ContextVar(
    "gateway_routing_enabled", default=None
)

# Backend-resolved X-Priority (0..5) for this grading run. Set at the worker
# entrypoint from `GradingConfig.resolved_priority` (or the validation-config
# response). Read by `Settings.priority_for_workload` / `apply_llm_target`.
# Single source of truth for the workload dict lives on the backend at
# rl-studio/server/utils/llm/main.py:_SERVICE_TO_PRIORITY (consumed by
# `resolve_priority`, which also folds in the LLM_GATEWAY_PRIORITY_OVERRIDE
# PostHog emergency-lane check). Defaults to None, which the resolver treats
# as P3 (middle) — matches the previous "unknown workload" behavior when an
# older server omits the field.
priority_ctx: ContextVar[int | None] = ContextVar(
    "llm_priority",
    default=None,
)

# Backend-threaded per-unit-of-work id for the LLM Gateway queue monitor
# (RLS-7655). Set once per grading run from the trajectory batch id (`batch_…`),
# else `{campaign_id}~{world_id}`, and emitted as X-Work-Unit so the gateway's
# per-unit Redis counters — and the queue-monitor hover card — attribute this
# run's calls. Defined here (not in `decorators`) to match `priority_ctx`.
# None → header omitted.
work_unit_ctx: ContextVar[str | None] = ContextVar(
    "llm_work_unit",
    default=None,
)


class Environment(Enum):
    LOCAL = "local"
    DEV = "dev"
    DEMO = "demo"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ENV: Environment = Environment.LOCAL

    # RL Studio API (grading config fetch + grading_logs webhook transport)
    RL_STUDIO_API: str | None = None
    RL_STUDIO_API_KEY: str | None = None

    SAVE_WEBHOOK_URL: str | None = None
    SAVE_WEBHOOK_API_KEY: str | None = None
    SCORE_WEBHOOK_URL: str | None = None

    # Persist grading logs via RL Studio internal API (requires
    # RL_STUDIO_API + RL_STUDIO_API_KEY when enabled).
    API_LOGGING: bool = False

    # Redis logging (live stream while grading run is active)
    REDIS_LOGGING: bool = False
    REDIS_HOST: str | None = None
    REDIS_PORT: int | None = None
    REDIS_USER: str | None = None
    REDIS_PASSWORD: str | None = None
    REDIS_STREAM_PREFIX: str = "grading_logs"

    # Datadog
    DATADOG_LOGGING: bool = False
    DATADOG_API_KEY: str | None = None
    DATADOG_APP_KEY: str | None = None

    # LiteLLM Proxy
    # If set, all LLM requests will be routed through the proxy
    LITELLM_PROXY_API_BASE: str | None = None
    LITELLM_PROXY_API_KEY: str | None = None

    # Mercor LLM Gateway (priority queue + backend abstraction in front of
    # the LiteLLM proxy). Active in DEV and PROD when secrets are present —
    # see is_gateway_routed. The backend resolves the per-(model, workload)
    # PostHog rule and threads the decision via gateway_routing_enabled_ctx.
    LLM_GATEWAY_API_BASE: str | None = None
    LLM_GATEWAY_API_KEY: str | None = None

    def is_gateway_routed(self) -> bool:
        """True only when the gateway is configured AND the backend resolved
        the LLM_GATEWAY_ROUTING PostHog flag as enabled for this grading
        run's (judge_model, workload) tuple.

        Four-layer gate, evaluated cheapest first. All must be true:

          1. ``ENV in {DEV, PROD}`` — only envs with their own gateway
             deployment can route. LOCAL / DEMO short-circuit to False.
          2. ``LLM_GATEWAY_API_BASE`` and ``LLM_GATEWAY_API_KEY`` are set.
          3. ``LLM_GATEWAY_API_BASE`` starts with ``https://`` — rejects
             plaintext + placeholder shapes seen in INC-293 telemetry.
          4. ``gateway_routing_enabled_ctx.get() is True`` — backend-resolved
             PostHog decision for this grading run's
             (judge_model, workload) tuple. ``None`` (older server) or
             ``False`` (rule didn't match) both fall through to LiteLLM.

        Mirrors the rl-studio backend gate at
        rl-studio/server/utils/llm/main.py:should_route_via_llm_gateway —
        keep both in lockstep.
        """
        if self.ENV not in (Environment.DEV, Environment.PROD):
            return False
        if not (self.LLM_GATEWAY_API_BASE and self.LLM_GATEWAY_API_KEY):
            return False
        if not self.LLM_GATEWAY_API_BASE.startswith("https://"):
            return False
        return gateway_routing_enabled_ctx.get() is True

    def priority_for_workload(self, workload: str | None) -> int:
        """Resolve the X-Priority bucket (0..5) applied to gateway-bound calls.

        The backend runs the full resolution (PostHog override → workload
        dict → default P3) once per dispatch and threads the int here via
        `priority_ctx`. `workload` is retained in the signature for call-site
        parity with the metric tags but is not used for lookup — the dict
        lives on the backend now (single source of truth).

        Falls back to default P3 when the ctx is unset — that happens when
        an older server omits `resolved_priority`, or in a non-Modal
        invocation that skipped the entrypoint. Same middle-priority
        fallback as the prior "unknown workload" behavior.
        """
        # `workload` is backend-resolved via priority_ctx; kwarg kept for signature parity.
        del workload
        ctx_val = priority_ctx.get()
        return ctx_val if ctx_val is not None else 3

    def apply_llm_target(
        self,
        kwargs: dict[str, Any],
        *,
        fairness_key: str | None = None,
        workload: str | None = None,
        priority: int | None = None,
    ) -> None:
        """Set api_base / api_key / gateway-control headers on kwargs.

        Single redirect point — sites that previously read LITELLM_PROXY_*
        directly call this instead. When the gateway is in use, also
        injects X-Gateway-Backend / X-Priority / X-Fairness-Key. No-op
        when neither LITELLM_PROXY_* nor LLM_GATEWAY_* is configured.

        X-Priority resolution order:
          1. Explicit `priority` kwarg (0..5) — caller override wins locally
          2. Backend-threaded `priority_ctx` value (already includes PostHog
             LLM_GATEWAY_PRIORITY_OVERRIDE emergency-lane resolution)
          3. Default P3 (middle — older server / unset ctx)

        `workload` is unused for resolution — the server did that lookup at
        dispatch time. Kept in the signature for call-site parity.
        """
        del workload  # backend-resolved via priority_ctx
        if self.is_gateway_routed():
            kwargs["api_base"] = self.LLM_GATEWAY_API_BASE
            kwargs["api_key"] = self.LLM_GATEWAY_API_KEY
            hdrs = dict(kwargs.get("extra_headers") or {})
            hdrs.setdefault("X-Gateway-Backend", "litellm")
            if priority is not None:
                resolved = priority
            else:
                ctx_val = priority_ctx.get()
                resolved = ctx_val if ctx_val is not None else 3
            if not 0 <= resolved <= 5:
                raise ValueError(
                    f"priority must be int in 0..5 (0=highest, 5=lowest); got {resolved!r}"
                )
            hdrs.setdefault("X-Priority", f"P{resolved}")
            if fairness_key:
                hdrs.setdefault("X-Fairness-Key", fairness_key)
            # Queue-monitor per-unit breakdown (RLS-7655): X-Work-Unit keys the
            # gateway's per-unit Redis counters by the grading run's batch id (or
            # campaign~world). Observability-only — the gateway drops a
            # malformed/oversized value — so forward as-is.
            work_unit = work_unit_ctx.get()
            if work_unit:
                hdrs.setdefault("X-Work-Unit", work_unit)
            # RLS-7655 diagnostics: single-task runs are reaching the gateway
            # with no X-Work-Unit. Log the resolved value (None means the ctx
            # was never set at the worker entrypoint) to pinpoint the gap.
            logger.debug(f"queue_monitor X-Work-Unit resolved={work_unit}")
            # Per-attempt upstream timeout — see rfc/2026-07-01-llm-gateway-caller-supplied-timeout.md.
            # Pulled from `kwargs["timeout"]` (the same value litellm.acompletion
            # uses locally) so the gateway's Temporal start_to_close_timeout and
            # httpx client budget match this runner's own client timeout.
            call_timeout = kwargs.get("timeout")
            if isinstance(call_timeout, (int, float)) and call_timeout > 0:
                hdrs.setdefault("X-Gateway-Timeout-S", str(int(call_timeout)))
            kwargs["extra_headers"] = hdrs
        elif self.LITELLM_PROXY_API_BASE and self.LITELLM_PROXY_API_KEY:
            kwargs.setdefault("api_base", self.LITELLM_PROXY_API_BASE)
            kwargs.setdefault("api_key", self.LITELLM_PROXY_API_KEY)

    # Scraping / web content (used by ACE link verification)
    ACE_FIRECRAWL_API_KEY: str | None = None


@cache
def get_settings() -> Settings:
    return Settings()
