"""Run one grade as a standalone PROCESS, for a Modal Sandbox to boot (RLS-10005).

`python -m runner.grading_entrypoint` takes `GRADING_RUN_ID` / `TRAJECTORY_ID`
from the environment and grades, which is the shape a Sandbox can start and the
shape `runner/k8s_worker.py` and Studio's Docker dispatch already use
(`infra/k8s/gitops/apps/archipelago-grading/`,
`packages/islands/implementations/internal_platform/utils/docker_dispatch.py`).

`run_grading` here is a DELIBERATE COPY of the body in `modal_labs.py`
----------------------------------------------------------------------
Not an oversight, and not the end state. Two functions are now named
`run_grading`, so be precise about which one is untouched: the one in
`modal_labs.py` is byte-identical TO `origin/main` -- that file has a 0-line
diff on this branch. The one in THIS file is the copy, and it is modified (see
the divergences below). All 111 Modal Function lanes keep calling the
`modal_labs.py` one, so nothing here can change a grade that dispatches as a
Function.
That surface is wide (101 verifiers, several reaching live external platforms)
and largely untestable outside production, so it is not worth touching to save
a duplicate. The sandbox path gets its canary from the `GRADING_SANDBOX_DISPATCH`
flag instead, which rolls out one judge model at a time and falls back to the
Function spawn.

The copy costs the one bug this whole design exists to avoid: `k8s_worker.py`
drifted from `run_grading` and silently lost ~15 context vars, taking
LLM-Gateway priority routing, budget metering and spend attribution with it.
`tests/test_grading_entrypoint_parity.py` makes that drift a CI failure rather
than a silent divergence. **If you change one body, change both, and let that
test tell you what you missed.**

TODO(RLS-10005): collapse the duplication -- **THIS FILE SURVIVES.** Delete the
590-line body inside `modal_labs.py` and reduce its `run_grading` to a wrapper
that delegates here (in-body import, never module scope). Do NOT delete this
file: it is what a Sandbox boots, so removing it takes the sandbox path with it.
The TRIGGER is `GRADING_SANDBOX_DISPATCH` reaching
100% and holding for a few days -- at that point this body is already carrying
all batch grading in prod, so pointing the remaining Function lanes at it is
SAFER than leaving them on a body nothing else exercises. Doing it before then
inverts that. `tests/test_grading_entrypoint_parity.py` goes in the same change,
since one body cannot drift from itself. Grep this ticket id for the full set.

WHY THE BODY CANNOT SIMPLY BE IMPORTED FROM `modal_labs.py`
-----------------------------------------------------------
Importing that file builds an image definition and an App, and requires
`modal` -- which a Sandbox has neither reason nor means to pay for. `modal
deploy` also imports it LOCALLY on the deploy runner with only Modal tokens and
no `.env`, which is why every grading import in it sits inside a function body
behind `# import-check-ignore`, and why
`rl-studio/server/linting/modal_deploy_import_checks.py` exists.

THIS MODULE MUST NOT IMPORT `modal`
-----------------------------------
`modal` is NOT a dependency of `archipelago/grading`. CI injects it with
`uv run --with modal`, and `pyproject.toml` excludes the three files that import
it from type-checking for exactly that reason. A Function *container* is handed
the Modal client by the platform whatever the image holds; a **Sandbox is not**,
following from sandboxes being denied Modal credentials by design (confirmed by
Modal, 2026-09-08). `modal_helpers` is already modal-free, so the only two
couplings in this body were `current_function_call_id()` and two exception
types, and both are arguments here. An `import modal` would break the sandbox
at runtime and the type check at desk.

Keep the in-body imports in-body, for the same reason they are in-body there.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable


async def run_grading(
    grading_run_id: str,
    trajectory_id: str,
    *,
    run_handle: str | None = None,
    cancelled_exceptions: tuple[type[BaseException], ...] = (),
    read_budget_stop: Callable[[], dict[str, object] | None] | None = None,
) -> None:
    """Run one grade end to end: fetch, download, verify, score, save.

    Args:
        grading_run_id: The grading run to run.
        trajectory_id: The trajectory to grade.
        run_handle: Opaque id of whatever is running this, for the log context.
            The Function path passes Modal's current function-call id; the
            sandbox path passes ``MODAL_TASK_ID``. Logged under the key
            ``function_call_id`` on BOTH paths -- the name is kept for
            continuity with every existing Datadog query, so do not rename it
            without checking what filters on it.
        cancelled_exceptions: Exception types to classify as CANCELLED rather
            than ERROR, on top of ``asyncio.CancelledError``, which is always
            included. The Function path passes Modal's ``FunctionTimeoutError``
            and ``InputCancellation``. The sandbox path passes nothing, because
            neither can occur there: a sandbox timeout is exit 124 and a cancel
            is ``terminate()``, so the process dies rather than raising into us.
        read_budget_stop: How to read a budget denial back when saving. Defaults
            to ``budget_stop_ctx``, which is the LANE's channel and must stay
            so. A standalone process has to pass
            ``runner.utils.budget_stop_record.budget_stop`` instead: every
            verifier runs in its own task and ``asyncio.gather`` copies the
            context into each one, so a denial recorded inside the grade is
            invisible to a read outside it, and ``asyncio.run`` copies it again.
            ``runner/utils/llm.py`` sets both channels for exactly this reason.
            Getting this wrong is silent -- the webhook simply omits the stop
            and the batch keeps scheduling grades that will also be denied.

    Raises:
        Whatever the GRADE raised, after saving a terminal status. Re-raised so
        the caller exits non-zero, which is how ``Sandbox.poll()`` reports a
        failed grade.

        NOT a save failure. Those are caught, counted on
        ``studio.grading.save_results_errors``, logged, and swallowed, so a run
        whose grade succeeded but whose save failed still exits 0. That is the
        lane's behaviour too, faithfully copied; the dead-container sweep is
        what catches such a run, since it reaps any exited sandbox whose row is
        still active. Fixing it means changing both bodies at once, which is
        owed by RLS-10925 (where they become one).
    """
    import asyncio  # import-check-ignore
    import time  # import-check-ignore
    import traceback  # import-check-ignore
    from typing import cast  # import-check-ignore

    from loguru import logger  # import-check-ignore

    # import-check-ignore
    from modal_helpers import (  # import-check-ignore
        SnapshotPrefix,
        _file_size,
        baseline_shape,
        download_golden_snapshots,
        download_snapshot,
        download_trajectory_snapshot,
        download_world_snapshot,
        fetch_grading_run_config,
        fetch_snapshot_ids,
        fetch_trajectory_output,
    )
    from runner.main import main  # import-check-ignore
    from runner.models import (  # import-check-ignore
        GradingRunStatus,
        ScoringMethodResult,
        VerifierResult,
    )
    from runner.save.main import save  # import-check-ignore
    from runner.utils.decorators import (  # import-check-ignore
        account_id_ctx,
        actor_email_ctx,
        actor_ip_ctx,
        actor_user_id_ctx,
        budget_enabled_ctx,
        budget_stop_ctx,
        campaign_id_ctx,
        first_llm_seen_ctx,
        flow_prefix_ctx,
        flow_started_at_ctx,
        flow_tags_ctx,
        impersonation_enabled_ctx,
        model_rates_ctx,
        synth_spend_purpose_ctx,
        task_id_ctx,
        trajectory_batch_id_ctx,
        trajectory_id_ctx,
        triggered_by_ctx,
        world_id_ctx,
    )
    from runner.utils.grading_log import logger as grading_logger  # import-check-ignore
    from runner.utils.logging.main import (  # import-check-ignore
        setup_logger,
        teardown_logger,
    )
    from runner.utils.metrics import (  # import-check-ignore
        PhaseHandle,
        distribution,
        grading_dims_ctx,
        increment,
        peak_memory_bytes,
        phase,
        snapshot_size_bucket,
    )
    from runner.utils.s3_transfer import (  # import-check-ignore
        backend_used,
        loose_timing,
        s3_backend_ctx,
    )
    from runner.utils.settings import (  # import-check-ignore
        gateway_routing_enabled_ctx,
        priority_ctx,
        work_unit_ctx,
    )

    GRADING_PREFIX = "studio.grading"

    def _base_tags(extra: list[str] | None = None) -> list[str]:
        # Per-run ids stay out of metric tags; logs and traces carry them.
        tags: list[str] = []
        if extra:
            tags.extend(extra)
        return tags

    def _emit_download_throughput(p: PhaseHandle, t0: float, size_bytes: float) -> None:
        """Emit bytes + MB/s siblings of `snapshot_download_seconds` so
        size-mix doesn't confound duration-based throughput analysis."""
        elapsed = max(time.perf_counter() - t0, 1e-6)  # div-by-zero guard
        p.value("snapshot_download_bytes", size_bytes)
        p.value("snapshot_download_mbps", size_bytes / 1e6 / elapsed)
        # The single-snapshot s5cmd loose paths (post_populate/world/trajectory)
        # record a (cp, pack) split so the A/B can attribute latency to transfer
        # vs serial STORED-zip packing. boto3, the prebuilt-archive path, and the
        # concurrent golden fan-out leave it at (0, 0) — skip so we don't emit
        # zeros (or, for goldens, a sum that wouldn't reconcile with the phase
        # wall-clock; see download_golden_snapshots).
        cp_seconds, pack_seconds = loose_timing()
        if cp_seconds or pack_seconds:
            p.value("snapshot_download_cp_seconds", cp_seconds)
            p.value("snapshot_download_pack_seconds", pack_seconds)

    run_grading_start = time.perf_counter()

    # Flow-scoped SLI ctxvars consumed by `@with_retry` in
    # runner/utils/decorators.py. Setting `flow_prefix_ctx` explicitly to
    # "studio.grading" (also its default) makes the namespace contract
    # self-documenting at the call site — mirror of agents/modal_labs.py.
    flow_started_at_token = flow_started_at_ctx.set(run_grading_start)
    flow_tags_token = flow_tags_ctx.set(_base_tags())
    flow_prefix_token = flow_prefix_ctx.set("studio.grading")
    first_llm_seen_token = first_llm_seen_ctx.set(False)
    triggered_by_token = triggered_by_ctx.set(None)

    # Injected rather than read from Modal: this module must not import
    # `modal` (see the module docstring).
    function_call_id = run_handle

    grading_run_status: GradingRunStatus | None = None
    verifier_results: list[VerifierResult] | None = None
    scoring_results: ScoringMethodResult | None = None
    error: BaseException | None = None
    grading_dims_token = None

    with logger.contextualize(
        function_call_id=function_call_id,
        grading_run_id=grading_run_id,
    ):
        try:
            setup_logger()

            distribution(
                "studio.grading.runtime_boot_seconds",
                time.perf_counter() - run_grading_start,
                tags=_base_tags(),
            )

            logger.debug(
                f"Fetching metadata for grading_run_id={grading_run_id}, trajectory_id={trajectory_id}"
            )

            fetch_start = time.perf_counter()
            async with phase(
                "fetch_trajectory", prefix=GRADING_PREFIX, tags=_base_tags()
            ):
                trajectory = await fetch_trajectory_output(trajectory_id)

            async with phase("fetch_config", prefix=GRADING_PREFIX, tags=_base_tags()):
                (
                    grading_settings,
                    verifiers,
                    eval_configs,
                    scoring_config,
                    trajectory_metadata,
                    skip_trajectory_verifiers,
                ) = await fetch_grading_run_config(grading_run_id)
                trajectory = trajectory.model_copy(
                    update=trajectory_metadata.model_dump(
                        exclude_defaults=True,
                        exclude_none=True,
                    )
                )
                # Surface batch membership for LLM Gateway priority routing:
                # set → grading_batch (P1), unset → grading_single (P0).
                trajectory_batch_id_ctx.set(trajectory_metadata.trajectory_batch_id)
                # Batch-spend guardrail: metering decided server-side per batch
                # (LLM_BUDGET_GUARDRAIL), threaded via the grading config.
                budget_enabled_ctx.set(bool(grading_settings.budget_metering_enabled))
                # Server-resolved judge rate card for spend metering.
                model_rates_ctx.set(grading_settings.llm_judge_model_rates)
                # Campaign id drives LLM Gateway X-Fairness-Key so grading
                # traffic interleaves across campaigns within the same
                # priority bucket. None when the server is older than the
                # field — the runner falls back to omitting the header.
                campaign_id_ctx.set(trajectory_metadata.campaign_id)
                # Owning account for the campaign — CAS spend attribution.
                # None when the server is older than the field.
                account_id_ctx.set(trajectory_metadata.account_id)
                # Owning task for the graded trajectory — CAS `task_id`
                # drill-down attribution. None when the server is older than
                # the field or the trajectory has no owning task.
                task_id_ctx.set(trajectory_metadata.task_id)
                # Trajectory being graded — attached as the `trajectory_id`
                # spend-attribution header so grading LLM cost rows join to the
                # trajectory (and its task) in the Spend V2 dashboard. world_id
                # is set below, once the dispatched/fallback value is resolved.
                trajectory_id_ctx.set(trajectory_id)
                # Acting user attribution for the grading run's outbound
                # external-platform API calls (per-user rate-limit
                # buckets). None when the run has no attributable user or
                # the server is older than the field — attribution omitted.
                actor_email_ctx.set(trajectory_metadata.studio_actor_email)
                # Acting user's id — companion to actor_email_ctx above; CAS
                # spend attribution needs the id, not just the email.
                actor_user_id_ctx.set(trajectory_metadata.studio_actor_user_id)
                # Acting user's IP for the x-biome-impersonate-user-ip header.
                actor_ip_ctx.set(trajectory_metadata.studio_actor_ip)
                # Backend-resolved (SPARTA_TAIGA_IMPERSONATION, per campaign)
                # gate for whether Sparta/Taiga calls attach
                # x-biome-impersonate-user. None → False (service-account,
                # pre-#13344). Gates ONLY the header.
                impersonation_enabled_ctx.set(
                    bool(trajectory_metadata.taiga_impersonation_enabled)
                )
                # Server-resolved S3 download backend (PostHog flag) for the
                # snapshot-download seam in modal_helpers.
                s3_backend_ctx.set(trajectory_metadata.s3_transfer_backend)
                # Backend-resolved gateway routing decision (per
                # (judge_model, workload) PostHog rule). Worker cannot
                # call PostHog, so the backend resolves once and threads
                # the bool here. None → False in `is_gateway_routed`
                # (fail-closed) so older servers keep traffic on LiteLLM.
                gateway_routing_enabled_ctx.set(
                    trajectory_metadata.gateway_routing_enabled
                )
                # Backend-resolved X-Priority (0..5). Server runs the full
                # precedence — PostHog LLM_GATEWAY_PRIORITY_OVERRIDE →
                # workload dict → default P3 — and threads the int here.
                # `Settings.resolve_priority` reads this via `priority_ctx`.
                # None falls back to worker-default P3 (older server).
                priority_ctx.set(trajectory_metadata.resolved_priority)
                causal_origin = (trajectory_metadata.grading_run_args or {}).get(
                    "cas_triggered_by"
                )
                triggered_by_ctx.set(
                    causal_origin if isinstance(causal_origin, str) else None
                )
                synth_spend_purpose_ctx.set(trajectory_metadata.synth_spend_purpose)

            async with phase(
                "fetch_snapshot_ids", prefix=GRADING_PREFIX, tags=_base_tags()
            ):
                snapshot_ids = await fetch_snapshot_ids(trajectory_id)

            # Composite of the three fetches above; emitted directly because
            # it's not its own do-and-fail step.
            distribution(
                "studio.grading.fetch_total_seconds",
                time.perf_counter() - fetch_start,
                tags=_base_tags(),
            )
            distribution(
                "studio.grading.verifier_count",
                float(len(verifiers)),
                tags=_base_tags(),
            )

            logger.debug("Metadata fetched successfully")

            # Download baseline, trajectory, and golden snapshots concurrently.
            # These are independent S3 workloads; running them in parallel roughly
            # halves wall-clock time for large snapshots (~30 GiB each). Each
            # download retains its own phase() for per-kind Datadog metrics.
            logger.debug("Downloading snapshots from S3")
            snapshot_dl_start = time.perf_counter()

            post_populate_id = snapshot_ids.get("post_populate_snapshot_id")

            async def _download_baseline():
                if post_populate_id:
                    logger.debug(
                        f"Using post-populate snapshot as baseline: {post_populate_id}"
                    )
                    async with phase(
                        "snapshot_download",
                        prefix=GRADING_PREFIX,
                        tags=_base_tags(["kind:post_populate"]),
                    ) as p:
                        t0 = time.perf_counter()
                        # Cast, exactly as `k8s_worker` does on this same
                        # call: `snapshot_ids` is untyped JSON, so `.get` gives
                        # `str` where `download_snapshot` wants the
                        # `SnapshotPrefix` literal. Latent until now -- this body
                        # lived in `modal_labs.py`, which `pyproject.toml`
                        # excludes from type-checking.
                        snapshot = await download_snapshot(
                            post_populate_id,
                            prefix=cast(
                                SnapshotPrefix,
                                snapshot_ids.get("post_populate_snapshot_prefix")
                                or "trajectories",
                            ),
                        )
                        p.tag(f"backend:{backend_used()}")
                        _emit_download_throughput(p, t0, _file_size(snapshot))
                        return snapshot
                else:
                    async with phase(
                        "snapshot_download",
                        prefix=GRADING_PREFIX,
                        tags=_base_tags(["kind:world"]),
                    ) as p:
                        t0 = time.perf_counter()
                        snapshot = await download_world_snapshot(
                            snapshot_ids["world_snapshot_id"],
                            snapshot_ids["task_data_id"],
                            snapshot_ids.get("task_data_prefix", "tasks"),
                            # Subtraction is resolved only against a state
                            # that ran the removal hook; an agent-in-playground
                            # capture did not. NOT defaulted to "trajectories"
                            # like the download prefix below: absent means the
                            # server cannot say which it was, and guessing
                            # "trajectories" for a playground capture is the
                            # inversion `_resolves_markers` exists to prevent.
                            # Absent resolves nothing, i.e. the behaviour before
                            # subtraction existed.
                            snapshot_ids.get("trajectory_snapshot_prefix"),
                            # The server's own answer, which supersedes both
                            # prefixes when present — a continuation looks
                            # exactly like a first turn from the ids alone.
                            snapshot_ids.get("subtraction_resolved"),
                        )
                        p.tag(f"backend:{backend_used()}")
                        # Tagged on `kind:world` specifically because this is the
                        # phase whose four shapes it separates; see
                        # `baseline_shape` for what they are and why.
                        p.tag(f"baseline:{baseline_shape(snapshot_ids)}")
                        _emit_download_throughput(p, t0, _file_size(snapshot))
                        return snapshot

            async def _download_trajectory():
                async with phase(
                    "snapshot_download",
                    prefix=GRADING_PREFIX,
                    tags=_base_tags(["kind:trajectory"]),
                ) as p:
                    t0 = time.perf_counter()
                    snapshot = await download_trajectory_snapshot(
                        snapshot_ids["trajectory_snapshot_id"],
                        snapshot_ids.get("trajectory_snapshot_prefix")
                        or "trajectories",
                    )
                    p.tag(f"backend:{backend_used()}")
                    _emit_download_throughput(p, t0, _file_size(snapshot))
                    return snapshot

            async def _download_goldens():
                golden_ids = snapshot_ids.get("golden_snapshot_ids", [])
                if golden_ids:
                    async with phase(
                        "snapshot_download",
                        prefix=GRADING_PREFIX,
                        tags=_base_tags(["kind:golden"]),
                    ) as p:
                        t0 = time.perf_counter()
                        result = await download_golden_snapshots(golden_ids)
                        p.tag(f"backend:{backend_used()}")
                        p.value("golden_snapshot_count", len(golden_ids))
                        _emit_download_throughput(
                            p, t0, sum(_file_size(s) for s in result)
                        )
                        return result
                return await download_golden_snapshots(golden_ids)

            (
                world_snapshot_bytes,
                trajectory_snapshot_bytes,
                golden_snapshots,
            ) = await asyncio.gather(
                _download_baseline(),
                _download_trajectory(),
                _download_goldens(),
            )

            distribution(
                "studio.grading.snapshot_download_total_seconds",
                time.perf_counter() - snapshot_dl_start,
                tags=_base_tags(),
            )
            distribution(
                "studio.grading.snapshot_download_total_bytes",
                float(
                    _file_size(world_snapshot_bytes)
                    + _file_size(trajectory_snapshot_bytes)
                    + sum(_file_size(s) for s in golden_snapshots)
                ),
                tags=_base_tags(),
            )

            logger.debug("Snapshots downloaded successfully")

            # Prefer the dispatched world_id (matches the work_unit the server
            # resolved priority with); fall back to the first verifier's
            # world_id when the server didn't send it. Task-scoped verifiers
            # leave world_id null, so the dispatched value is what keeps
            # X-Work-Unit aligned with dispatch.
            _world_id = trajectory_metadata.world_id or next(
                (v.world_id for v in verifiers if v.world_id), None
            )
            # Resolved world is also the `world_id` spend-attribution header
            # (allowlisted in the LiteLLM proxy) so grading LLM cost rows slice
            # per-project in the Spend V2 dashboard. Set here — not at the
            # metadata block above — because task-scoped runs fall back to the
            # first verifier's world_id, which is only known after fetch.
            world_id_ctx.set(_world_id)
            # Queue monitor X-Work-Unit (RLS-7655): key by the graded run's
            # trajectory batch id (→ batch name), else `{campaign}~{world}`
            # (→ campaign · world). None only when neither is available. Set
            # OUTSIDE the dims try below so a snapshot-size/dims failure can't
            # skip it and leave grading gateway calls without X-Work-Unit.
            work_unit_ctx.set(
                trajectory_metadata.trajectory_batch_id
                or (
                    f"{trajectory_metadata.campaign_id}~{_world_id}"
                    if trajectory_metadata.campaign_id and _world_id
                    else None
                )
            )

            # Tag every downstream grading metric (verifier/scoring/total/peak
            # RSS) with the world and its size bucket, so they can be sliced by
            # world and by the snapshot size a grade actually pulled. Derived
            # here because snapshot size is only known post-download; set via a
            # ContextVar so the per-verifier child tasks inherit it.
            try:
                world_dims = [
                    f"snapshot_size_bucket:{snapshot_size_bucket(_file_size(world_snapshot_bytes))}"
                ]
                if _world_id:
                    world_dims.append(f"world_id:{_world_id}")
                grading_dims_token = grading_dims_ctx.set(world_dims)
            except Exception as e:
                logger.debug(f"Failed to set grading dims ctx: {e}")

            # Direct emit (not phase) — the inner main() handles its own
            # exceptions and converts them into a GradingRunStatus on the
            # output, so the wrapper here doesn't see them as raises and
            # the status tag should reflect the agent's reported outcome.
            eval_start = time.perf_counter()
            (
                grading_run_id,
                grading_run_status,
                verifier_results,
                scoring_results,
            ) = await main(
                grading_run_id=grading_run_id,
                trajectory_id=trajectory_id,
                initial_snapshot_bytes=world_snapshot_bytes,
                final_snapshot_bytes=trajectory_snapshot_bytes,
                trajectory=trajectory,
                grading_settings=grading_settings,
                verifiers=verifiers,
                eval_configs=eval_configs,
                scoring_config=scoring_config,
                golden_snapshots=golden_snapshots,
                skip_trajectory_verifiers=skip_trajectory_verifiers,
                grading_run_args=trajectory_metadata.grading_run_args,
            )
            distribution(
                "studio.grading.evaluation_seconds",
                time.perf_counter() - eval_start,
                tags=_base_tags([f"status:{grading_run_status.value}"]),
            )

            logger.info(
                f"Grading run {grading_run_id} completed with status {grading_run_status}"
            )

        except BaseException as e:
            logger.error(f"Error running grading: {repr(e)}\n{traceback.format_exc()}")
            error = e

        finally:
            # Determine final status based on error type
            # `cancelled_exceptions` is the caller's; CancelledError is always
            # ours. See the signature for why the split exists.
            if isinstance(error, (*cancelled_exceptions, asyncio.CancelledError)):
                grading_run_status = GradingRunStatus.CANCELLED
            elif error is not None:
                grading_run_status = GradingRunStatus.ERROR

            # Fallback if status is still None (defensive)
            if grading_run_status is None:
                grading_run_status = GradingRunStatus.ERROR

            try:
                logger.debug("Saving grading result")
                # Build final_scoring_results, adding error if needed
                if scoring_results is None:
                    result_values = {}
                    final_score = 0.0
                else:
                    result_values = {**scoring_results.scoring_method_result_values}
                    final_score = scoring_results.final_score

                if error and "error" not in result_values:
                    result_values["error"] = str(error)

                final_scoring_results = ScoringMethodResult(
                    final_score=final_score,
                    scoring_method_result_values=result_values,
                )

                save_start = time.perf_counter()
                # Batch-spend guardrail: if a judge LLM call was budget-denied, the
                # runner stashed a structured stop — forward it so the webhook can
                # halt the batch (grading-lane parity with the trajectory path).
                await save(
                    grading_run_id,
                    grading_run_status,
                    verifier_results or [],
                    final_scoring_results,
                    # Injected: the lane reads the contextvar, a standalone
                    # process must read the process-scoped record. See the
                    # signature.
                    budget_stop=(read_budget_stop or budget_stop_ctx.get)(),
                )
                distribution(
                    "studio.grading.save_results_seconds",
                    time.perf_counter() - save_start,
                    tags=_base_tags(),
                )
            except Exception as save_error:
                increment(
                    "studio.grading.save_results_errors",
                    tags=_base_tags([f"exc:{type(save_error).__name__}"]),
                )
                logger.error(
                    f"Failed to save result: {repr(save_error)}\n{traceback.format_exc()}"
                )

            try:
                summary_status = (
                    grading_run_status.value if grading_run_status else "unknown"
                )
                final_score_value = (
                    scoring_results.final_score if scoring_results else None
                )
                summary_tags = _base_tags([f"status:{summary_status}"])

                increment(
                    "studio.grading.completed",
                    tags=summary_tags,
                )
                distribution(
                    "studio.grading.total_seconds",
                    time.perf_counter() - run_grading_start,
                    tags=summary_tags,
                )
                if verifier_results is not None:
                    distribution(
                        "studio.grading.verifier_results_count",
                        float(len(verifier_results)),
                        tags=summary_tags,
                    )
                if final_score_value is not None:
                    distribution(
                        "studio.grading.final_score",
                        float(final_score_value),
                        tags=summary_tags,
                    )
                # Peak memory for the whole grading run, measured against the
                # container memory ceiling. Uses the cgroup peak so
                # subprocess-heavy verifiers (code_execution / db_code_verifier
                # / bua_verifier) are counted, not just the runner process.
                try:
                    peak_rss = peak_memory_bytes()
                    if peak_rss > 0:
                        distribution(
                            "studio.grading.peak_rss_bytes",
                            float(peak_rss),
                            tags=summary_tags,
                        )
                except Exception as e:
                    logger.debug(f"Failed to emit peak_rss_bytes: {e}")
            except Exception as e:
                logger.debug(f"Failed to emit grading summary metrics: {e}")

            # Reset flow-scoped ctxvars set at run_grading entry. Mirror of
            # agents/modal_labs.py — keeps the contract explicit even though
            # containers are single-use (single_use_containers=True).
            try:
                flow_tags_ctx.reset(flow_tags_token)
                flow_prefix_ctx.reset(flow_prefix_token)
                first_llm_seen_ctx.reset(first_llm_seen_token)
                flow_started_at_ctx.reset(flow_started_at_token)
                triggered_by_ctx.reset(triggered_by_token)
                if grading_dims_token is not None:
                    grading_dims_ctx.reset(grading_dims_token)
            except ValueError:
                # Token was set in a different Context; safe to skip.
                pass

            grading_logger.bind(
                message_type="grading_end",
                payload={
                    "status": grading_run_status.value if grading_run_status else None,
                    "final_score": (
                        scoring_results.final_score if scoring_results else None
                    ),
                },
            ).info("Grading run done")
            await teardown_logger()

            if error is not None:
                raise error


# Always needed: the ids to grade, and where to report the result.
#
# The webhook pair is here for the SILENT failure, which is the worse of the two
# this function guards. `runner/save/services/webhook.py` logs a warning and
# RETURNS when either is unset, so without them a grade downloads its snapshots,
# runs every verifier, spends the judge-LLM budget, exits 0, and writes nothing
# -- `Sandbox.poll()` then reports a successful grade that produced no result,
# and the run sits active until the dead-container sweep reaps it. The OIDC pair
# below at least raises. Not hypothetical on a preview deploy either: the
# grading App overrides SAVE_WEBHOOK_URL through an app-level Secret, which a
# Sandbox does not inherit unless its creator passes it (RLS-10005). k8s and
# Docker set both as well, so requiring them costs those paths nothing.
_REQUIRED_ENV = (
    "GRADING_RUN_ID",
    "TRAJECTORY_ID",
    "RL_STUDIO_API",
    "RL_STUDIO_API_KEY",
    "SAVE_WEBHOOK_URL",
    "SAVE_WEBHOOK_API_KEY",
)

# Needed only ON MODAL. `modal_helpers._get_s3_session` exchanges these two for
# short-lived AWS credentials, and a bare k8s/Docker run legitimately has
# neither -- there it falls through to an ambient aioboto3 session, which is
# why this is conditional rather than blanket.
_REQUIRED_ENV_ON_MODAL = ("MODAL_IDENTITY_TOKEN", "MODAL_OIDC_ROLE_ARN")

# TWO signals for "on Modal", because either alone is an unverified assumption
# about the platform. `MODAL_IS_REMOTE` is what `_get_s3_session` itself keys on
# to choose between raising and falling back, but nothing in this repo SETS it
# -- every occurrence is a read -- so whether Modal sets it inside a Sandbox as
# it does in a Function container is not something the code can answer.
# `MODAL_TASK_ID` is the second: already load-bearing in this file for
# `run_handle` below, on the same understanding that Modal sets it in every
# container it runs. `MODAL_SANDBOX_ID` is the third, added because review
# reported that Modal sets the task and sandbox ids inside a Sandbox while
# MODAL_IS_REMOTE is documented for Function containers -- which, if right,
# means the single-signal version of this check was silent in every sandbox,
# the exact case it exists for.
#
# Requiring the pair when ANY is present means the check only goes quiet if all
# three are absent. None exists off Modal, so the k8s/Docker fallback is
# untouched. Raised by review on #21672, in three passes: the signal set, then
# the trimming, then which variables may be trimmed at all.
# MODAL_IS_REMOTE is tested UNTRIMMED, exactly as `_get_s3_session` tests it
# (`modal_helpers.py:173`). That is deliberate and it is the whole point: this
# preflight must never be LOOSER than the thing it protects. Trimming it made
# a whitespace-only value read as off-Modal here while the consumer still read
# it as on-Modal and raised -- so the grade started and died opaquely inside
# the first snapshot download, which is what the preflight exists to prevent.
_ON_MODAL_UNTRIMMED = ("MODAL_IS_REMOTE",)

# The id signals are TRIMMED. Nothing downstream branches on them; they are
# identity, and a blank one carries no information. Reading whitespace here as
# "on Modal" would demand OIDC credentials from a bare k8s/Docker deployment
# whose ambient AWS credentials are fine, and refuse to start it.
_ON_MODAL_TRIMMED = ("MODAL_TASK_ID", "MODAL_SANDBOX_ID")


def _missing_env() -> list[str]:
    """Names of the variables this process needs and does not have.

    NAMES ONLY, never values: this is the one place that sees an unredacted
    credential environment, and its whole output goes into an exception message.

    A Sandbox receives MODAL_IDENTITY_TOKEN only if its creator passed
    `include_oidc_identity_token=True`; a Function gets it from the platform.
    Every other sandbox creator in the server passes False, so getting this
    wrong is the likely mistake, and without this check the symptom is an opaque
    credentials failure inside the first snapshot download rather than a named
    variable at startup.
    """
    required = list(_REQUIRED_ENV)
    # Per-variable, not uniform: see the two tuples above. Whichever way this
    # is written uniformly, one of the two failure modes comes back.
    on_modal = any(os.environ.get(name) for name in _ON_MODAL_UNTRIMMED) or any(
        (os.environ.get(name) or "").strip() for name in _ON_MODAL_TRIMMED
    )
    if on_modal:
        required += list(_REQUIRED_ENV_ON_MODAL)
    return [name for name in required if not (os.environ.get(name) or "").strip()]


async def _main() -> None:
    """Process entrypoint: ids from the environment, as k8s and Docker pass them."""
    missing = _missing_env()
    if missing:
        # One error naming ALL of them, not the first: a misconfigured deploy is
        # usually missing several, and fixing them one boot at a time is the
        # slow way to find that out.
        raise RuntimeError(
            f"grading entrypoint is missing required environment: {', '.join(missing)}"
        )

    grading_run_id = (os.environ.get("GRADING_RUN_ID") or "").strip()
    trajectory_id = (os.environ.get("TRAJECTORY_ID") or "").strip()

    # The lane each Function sets with `lane_ctx.set(...)` in its own body.
    # Without it every metric this run emits is attributed to no lane, which is
    # the dimension the debugging runbook tells you to group by.
    lane = (os.environ.get("GRADING_LANE") or "").strip()
    if lane:
        from runner.utils.metrics import lane_ctx  # import-check-ignore

        lane_ctx.set(lane)

    # Before the grade, so a denial can never outlive the run that produced
    # it. `budget_stop_record` is process-scoped and this process runs one grade.
    from runner.utils.budget_stop_record import (  # import-check-ignore
        budget_stop,
        reset_budget_stop,
    )

    reset_budget_stop()

    await run_grading(
        grading_run_id,
        trajectory_id,
        # Modal sets MODAL_TASK_ID in every container it runs, sandboxes
        # included. None when absent (a bare docker/k8s run), which costs only
        # the log field.
        run_handle=(os.environ.get("MODAL_TASK_ID") or "").strip() or None,
        # Empty on purpose: see `run_grading`'s signature. Neither Modal
        # exception can reach a sandbox.
        cancelled_exceptions=(),
        # NOT `budget_stop_ctx`: a denial raised inside a verifier task cannot
        # reach a read out here. Devin caught this on #21672.
        read_budget_stop=budget_stop,
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
