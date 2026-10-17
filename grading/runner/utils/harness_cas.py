"""CAS ledger rows for an agent harness grading run, built from its terminal usage.

A harness such as the agentic verifier's Claude Code runs a CLI that calls the
provider itself, so ``call_llm``'s CAS emit never sees those calls; the SDK's
result usage is the only record this process gets (INFR-3584, after INFR-3579).
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from loguru import logger

from runner.utils.cas_ledger_emit import (
    record_cas_failure,
    record_cas_success,
    tags_from_spend_headers,
)
from runner.utils.decorators import (
    account_id_ctx,
    actor_user_id_ctx,
    campaign_id_ctx,
    pipeline_run_id_ctx,
    qc_audit_id_ctx,
    subject_kind_ctx,
    synth_spend_purpose_ctx,
    task_id_ctx,
    trajectory_batch_id_ctx,
    trajectory_id_ctx,
    triggered_by_ctx,
    world_id_ctx,
)
from runner.utils.grading_log import grading_run_id_ctx
from runner.utils.settings import work_unit_ctx

_TOKEN_FIELDS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "inputTokens"),
    ("output_tokens", "outputTokens"),
    ("cache_read_input_tokens", "cacheReadInputTokens"),
    ("cache_creation_input_tokens", "cacheCreationInputTokens"),
)

# `claude-opus-5[1m]` names a context window, not a rate band.
_CONTEXT_WINDOW_SUFFIX = re.compile(r"\[[^\]]*\]$")


def grading_harness_tags(service: str) -> dict[str, str]:
    """The CAS tags ``call_llm`` would send for this grading run, under *service*.

    Read from the same ContextVars ``call_llm`` builds its spend headers from, so a
    harness's rows slice by campaign, task and grading run like every other grading call.
    """
    batch_id = trajectory_batch_id_ctx.get()
    triggered_by = triggered_by_ctx.get()
    headers: dict[str, str | None] = {
        "service": service,
        "workload": "grading_batch" if batch_id else "grading_single",
        "work_unit": work_unit_ctx.get(),
        "grading_run_id": grading_run_id_ctx.get(),
        "campaign_id": campaign_id_ctx.get(),
        "task_id": task_id_ctx.get(),
        "account_id": account_id_ctx.get(),
        "user_id": actor_user_id_ctx.get(),
        "world_id": world_id_ctx.get(),
        "trajectory_id": trajectory_id_ctx.get(),
        "batch_id": batch_id,
        "qc_audit_id": qc_audit_id_ctx.get(),
        "subject_kind": subject_kind_ctx.get(),
        "triggered_by": triggered_by,
        "pipeline_run_id": pipeline_run_id_ctx.get(),
        # Same condition call_llm applies: synth spend keeps its delivery/R&D split.
        "purpose": synth_spend_purpose_ctx.get()
        if service == "synthetic" or triggered_by == "synth"
        else None,
    }
    return tags_from_spend_headers({k: v for k, v in headers.items() if v})


def _token_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _field(entry: Any, name: str) -> Any:
    """The SDK has reported usage as both a mapping and an object, so read either."""
    return entry.get(name) if isinstance(entry, dict) else getattr(entry, name, None)


def _tokens(entry: Any) -> dict[str, int]:
    return {
        native: _token_count(
            _field(entry, native)
            if _field(entry, native) is not None
            else _field(entry, camel)
        )
        for native, camel in _TOKEN_FIELDS
    }


def _usage_by_model(raw: dict[str, Any], model: str) -> dict[str, dict[str, int]]:
    """Billed tokens per model: the SDK's per-model split, else the run total, else what streamed."""
    per_model = raw.get("model_usage") or raw.get("modelUsage")
    if isinstance(per_model, dict):
        by_model: dict[str, dict[str, int]] = {}
        for label, entry in per_model.items():
            if not isinstance(entry, dict):
                continue
            tokens = _tokens(entry)
            if not sum(tokens.values()):
                continue
            name = _CONTEXT_WINDOW_SUFFIX.sub("", str(label).strip()) or model
            merged = by_model.setdefault(name, dict.fromkeys(tokens, 0))
            for key, value in tokens.items():
                merged[key] += value
        if by_model:
            return by_model

    usage = raw.get("usage")
    if usage is not None and not isinstance(usage, str | bytes):
        tokens = _tokens(usage)
        if sum(tokens.values()):
            return {model: tokens}

    streamed = raw.get("stream_usage")
    if isinstance(streamed, dict) and streamed:
        return _usage_by_model(
            {"model_usage": _model_usage_from_messages(streamed)}, model
        )
    return {}


def _model_usage_from_messages(
    by_message: dict[str, tuple[str, Any]],
) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {}
    for message_model, usage in by_message.values():
        tokens = _tokens(usage)
        merged = totals.setdefault(message_model, dict.fromkeys(tokens, 0))
        for key, value in tokens.items():
            merged[key] += value
    return totals


def emit_harness_run(
    raw: dict[str, Any],
    *,
    model: str,
    tags: dict[str, str],
    latency_ms: int | None,
    error_code: str | None = None,
) -> None:
    """One CAS row per billed model, or one failed row when no usage came back. Never raises.

    The usage is Anthropic-native: ``input_tokens`` excludes the cache tiers, which
    the CAS client adds back. Pass *error_code* for an attempt that raised: its
    usage-bearing rows are ``partial``, and with no usage it is one failed row.
    """
    try:
        by_model = _usage_by_model(raw, model)
        logger.info(
            f"harness CAS emit: service={tags.get('service')} rows={len(by_model) or 1} "
            f"tokens={sum(sum(t.values()) for t in by_model.values())} "
            f"error={error_code or '-'}"
        )
        if not by_model:
            record_cas_failure(
                model=model,
                tags=tags,
                backend="litellm",
                latency_ms=latency_ms,
                error_code=error_code or "no_usage",
            )
            return
        for row_model, tokens in by_model.items():
            record_cas_success(
                SimpleNamespace(
                    model=row_model, id=None, usage=SimpleNamespace(**tokens)
                ),
                model=row_model,
                tags=tags,
                backend="litellm",
                latency_ms=latency_ms,
                status="partial" if error_code else "success",
                error_code=error_code,
            )
    except Exception:  # noqa: BLE001 — attribution must not fail the grade
        logger.warning("harness CAS emit skipped after an error")
