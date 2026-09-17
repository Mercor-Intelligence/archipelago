"""Post-populate hooks tolerate an app that refuses the pinned runtime actor.

The failure this exists for: a world's persona (`EXPERT_MODAL_ACTOR_EMAIL`)
had no account in one of the apps the world mounted. That app's seed loader
read its seed back through its own REST API as the actor, got a 401, and
exited non-zero — which 500d `/data/populate/s3` and killed the provision for
every OTHER app on the platform. 317 trajectories died at 0s elapsed.

A persona legitimately may not exist in every app (a sales role has no
GitHub), so that app is now reported unavailable and the provision continues.
The other half of the contract matters just as much: any failure that is NOT
an identity rejection must still fail loudly, or a broken database restore
quietly becomes a missing app.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from loguru import logger

from runner.data.populate import main as populate_main
from runner.data.populate.main import (
    _identity_rejection_reason,
    run_lifecycle_hooks,
    run_post_populate_hooks,
)
from runner.data.populate.models import (
    LifecycleHook,
    PopulateRequest,
    PopulateResult,
    PopulateSource,
)

_A_SOURCE = PopulateSource(
    url="s3://snapshots/worlds/w_1/filesystem/", subsystem="filesystem"
)

# The real stderr shape, from an app's seed-loader read-back.
_ACTOR = "adammares@thednvr.com"
_REJECTION = (
    "[verify] /api/users: HTTP 401 (code='unauthorized', "
    f"message='No user found with email: {_ACTOR}')"
)


def _rejecting_hook(name: str = "deeptune_app") -> LifecycleHook:
    return LifecycleHook(
        name=name,
        command=f'echo "{_REJECTION}" >&2; exit 1',
        env={"EXPERT_MODAL_ACTOR_EMAIL": _ACTOR},
    )


def _broken_hook(name: str = "mysql") -> LifecycleHook:
    return LifecycleHook(
        name=name, command="echo 'ERROR 1045: table dump corrupt' >&2; exit 2"
    )


def _ok_hook(name: str) -> LifecycleHook:
    return LifecycleHook(name=name, command="echo populated")


_UNAVAILABLE_METRIC = "studio.trajectory.populate_app_unavailable"


def _capture_unavailable_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, float, list[str] | None]]:
    """Collect every `populate_app_unavailable` point, ignoring phase timings.

    A distribution rather than a count on purpose — see the call site — so this
    also pins the value at 1.0: the metric is read as `count:`, and a point
    carrying a duration would read as a rate of apps lost.
    """
    points: list[tuple[str, float, list[str] | None]] = []
    real = populate_main.distribution

    def _fake_distribution(
        metric: str, value: float, tags: list[str] | None = None
    ) -> None:
        if metric == _UNAVAILABLE_METRIC:
            points.append((metric, value, tags))
            return
        real(metric, value, tags)

    monkeypatch.setattr(populate_main, "distribution", _fake_distribution)
    return points


class TestIdentityClassification:
    """`_identity_rejection_reason` is the whole guardrail; pin its edges."""

    def test_matches_the_real_rejection_and_keeps_the_address(self) -> None:
        reason = _identity_rejection_reason(RuntimeError(_REJECTION), _ACTOR)
        assert reason is not None
        assert "adammares@thednvr.com" in reason

    def test_picks_the_401_line_out_of_a_noisy_hook(self) -> None:
        noisy = RuntimeError(
            "Lifecycle hook 'app' failed with exit code 1: "
            f"loading seed...\nwrote 412 rows\n{_REJECTION}\ndone"
        )
        reason = _identity_rejection_reason(noisy, _ACTOR)
        assert reason is not None
        assert reason.startswith("[verify] /api/users")

    def test_ignores_an_unrelated_failure(self) -> None:
        assert (
            _identity_rejection_reason(RuntimeError("table dump corrupt"), _ACTOR)
            is None
        )

    def test_a_bare_401_is_not_enough(self) -> None:
        """Needs a cause too — a hook echoing a port or count must not match."""
        assert (
            _identity_rejection_reason(RuntimeError("listening on HTTP 401"), _ACTOR)
            is None
        )

    def test_an_unauthorized_without_401_is_not_enough(self) -> None:
        assert (
            _identity_rejection_reason(RuntimeError("unauthorized push"), _ACTOR)
            is None
        )

    @pytest.mark.parametrize(
        "stderr",
        [
            "GET /seed: HTTP 401 Unauthorized",
            "401 Unauthorized",
            "boto3 error fetching dump: HTTP 401 Unauthorized (token expired)",
            "urllib.error.HTTPError: HTTP Error 401: Unauthorized",
        ],
    )
    def test_a_plain_401_unauthorized_still_fails_the_populate(
        self, stderr: str
    ) -> None:
        """The standard HTTP reason phrase must NOT read as a missing person.

        "HTTP 401 Unauthorized" satisfies a status test for "401" and a cause
        test for the word "unauthorized" all by itself. If that counted, a hook
        dying on an expired credential would be filed as "the persona has no
        account here" and the app silently dropped — a loud failure converted
        into quiet bad data. The cause has to say the USER does not exist.
        """
        assert _identity_rejection_reason(RuntimeError(stderr), _ACTOR) is None

    def test_a_missing_user_without_a_401_is_not_enough(self) -> None:
        """Both signals, still. A restore log naming a user is not a rejection."""
        assert (
            _identity_rejection_reason(
                RuntimeError("ERROR 1449: user not found for definer of view v_x"),
                _ACTOR,
            )
            is None
        )

    def test_two_unrelated_lines_do_not_vouch_for_each_other(self) -> None:
        """The signals must describe ONE operation, so they must share a line.

        A hook runs several commands and their stderr is folded into a single
        exception. Here a MySQL view-definer warning supplies "user not found"
        and a totally separate S3 download supplies the 401. Neither says the
        persona was rejected, so tolerating this would suppress a real expired
        credential and provision the app with no data.
        """
        combined = RuntimeError(
            "Lifecycle hook 'mysql' failed with exit code 1: "
            "ERROR 1449: user not found for definer of view v_x\n"
            "boto3 download failed: HTTP 401 Unauthorized (token expired)"
        )
        assert _identity_rejection_reason(combined, _ACTOR) is None

    def test_a_split_rejection_fails_closed(self) -> None:
        """Status on one line, cause on the next: not matched, so it raises.

        Narrower than strictly necessary, and deliberately so — an unmatched
        rejection fails the populate exactly as it does today.
        """
        split = RuntimeError(
            "[verify] /api/users: HTTP 401\n  No user found with email: a@b.c"
        )
        assert _identity_rejection_reason(split, _ACTOR) is None

    def test_a_different_missing_user_is_not_our_rejection(self) -> None:
        """A 401 about somebody else is a real failure, not an absent persona.

        A seed hook authenticates more than one identity. Here the persona
        verifies fine and an integration's service account is the one missing,
        so the app is broken in a way that must fail the populate.
        """
        other = RuntimeError(
            "[setup] /api/integrations: HTTP 401 unknown user sync-bot@acme.test"
        )
        assert _identity_rejection_reason(other, _ACTOR) is None

    def test_no_known_actor_fails_closed(self) -> None:
        """Nothing to compare against, so nothing may be tolerated."""
        assert _identity_rejection_reason(RuntimeError(_REJECTION), None) is None

    def test_a_blank_hook_override_does_not_borrow_the_container_actor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hook pinned to "" ran as nobody, so nothing may be tolerated.

        `run_env.update(hook.env)` overrides with an empty string as well, so
        reading past it to the container's address would attribute a rejection
        to a person this hook never acted as.
        """
        monkeypatch.setenv("EXPERT_MODAL_ACTOR_EMAIL", _ACTOR)
        blank = LifecycleHook(
            name="app", command="true", env={"EXPERT_MODAL_ACTOR_EMAIL": ""}
        )
        assert populate_main._pinned_actor_email(blank) is None

    def test_the_container_actor_is_used_when_the_hook_omits_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EXPERT_MODAL_ACTOR_EMAIL", _ACTOR)
        omitted = LifecycleHook(name="app", command="true", env={"OTHER": "x"})
        assert populate_main._pinned_actor_email(omitted) == _ACTOR

    def test_the_carrier_is_never_read_as_the_actor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sandbox's carrier is not what any hook ran as.

        The sandbox is handed the persona under `MERCOR_STUDIO_ENV__…` and
        `start.sh` copies it onto the app-facing name only inside each SERVICE
        subshell — never for a hook. So a hook that did not get the app-facing
        name ran as nobody, and reading the carrier here would attribute the
        rejection to a person this hook never acted as.
        """
        monkeypatch.delenv("EXPERT_MODAL_ACTOR_EMAIL", raising=False)
        monkeypatch.setenv("MERCOR_STUDIO_ENV__EXPERT_MODAL_ACTOR_EMAIL", _ACTOR)
        unpinned = LifecycleHook(name="app", command="true", env={"OTHER": "x"})
        assert populate_main._pinned_actor_email(unpinned) is None

    def test_the_carrier_alone_makes_a_lost_merge_detectable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not readable as the actor, but readable as "the merge was lost"."""
        monkeypatch.delenv("EXPERT_MODAL_ACTOR_EMAIL", raising=False)
        monkeypatch.setenv("MERCOR_STUDIO_ENV__EXPERT_MODAL_ACTOR_EMAIL", _ACTOR)
        assert populate_main._sandbox_was_given_an_actor() is True

    def test_a_blank_carrier_is_not_a_lost_merge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A task that carries no studio env must not log a lost merge.

        Which is every task until a world opts in, so a truthiness read here is
        the difference between silence and an error on every failed hook.
        """
        monkeypatch.setenv("MERCOR_STUDIO_ENV__EXPERT_MODAL_ACTOR_EMAIL", "")
        assert populate_main._sandbox_was_given_an_actor() is False

    def test_no_carrier_at_all_is_not_a_lost_merge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MERCOR_STUDIO_ENV__EXPERT_MODAL_ACTOR_EMAIL", raising=False)
        assert populate_main._sandbox_was_given_an_actor() is False

    def test_reason_is_bounded(self) -> None:
        """A hook that dumps a novel to stderr must not ride onto the payload."""
        flood = RuntimeError("x" * 10_000 + f"\n{_REJECTION}")
        reason = _identity_rejection_reason(flood, _ACTOR)
        assert reason is not None
        assert len(reason) <= 500


class TestPostPopulateTolerance:
    async def test_rejecting_app_is_skipped_and_siblings_still_populate(self) -> None:
        timings, unavailable = await run_post_populate_hooks(
            [_ok_hook("crm"), _rejecting_hook(), _ok_hook("email")]
        )
        assert [t.name for t in timings] == ["crm", "email"]
        assert [u.name for u in unavailable] == ["deeptune_app"]
        assert "adammares@thednvr.com" in unavailable[0].reason

    async def test_a_real_failure_still_raises(self) -> None:
        """The guardrail: only identity rejections degrade."""
        with pytest.raises(RuntimeError, match="table dump corrupt"):
            await run_post_populate_hooks([_ok_hook("crm"), _broken_hook()])

    async def test_a_real_failure_wins_over_a_tolerated_one(self) -> None:
        """A run that also hit a real error is failed, not degraded."""
        with pytest.raises(RuntimeError, match="table dump corrupt"):
            await run_post_populate_hooks([_rejecting_hook(), _broken_hook()])

    async def test_single_rejecting_hook_is_tolerated(self) -> None:
        """The one-hook path used to bypass the gather entirely — cover it."""
        timings, unavailable = await run_post_populate_hooks([_rejecting_hook()])
        assert timings == []
        assert [u.name for u in unavailable] == ["deeptune_app"]

    async def test_single_broken_hook_still_raises(self) -> None:
        with pytest.raises(RuntimeError, match="table dump corrupt"):
            await run_post_populate_hooks([_broken_hook()])

    async def test_every_rejecting_app_is_named(self) -> None:
        timings, unavailable = await run_post_populate_hooks(
            [_rejecting_hook("a"), _rejecting_hook("b"), _ok_hook("c")]
        )
        assert [t.name for t in timings] == ["c"]
        assert sorted(u.name for u in unavailable) == ["a", "b"]

    async def test_no_hooks_is_empty(self) -> None:
        assert await run_post_populate_hooks([]) == ([], [])

    async def test_clean_run_reports_nothing_unavailable(self) -> None:
        timings, unavailable = await run_post_populate_hooks(
            [_ok_hook("crm"), _ok_hook("email")]
        )
        assert len(timings) == 2
        assert unavailable == []

    async def test_an_app_that_seeded_nobody_is_tolerated_too(self) -> None:
        """A KNOWN limit, pinned so it cannot change by accident.

        An empty user table — a truncated CSV, a restore that fails but exits
        0, a seed-ordering bug — refuses the actor with the same line as a
        persona who was never meant to be there. The app is broken, not
        app-less, and nothing in the runner can tell. Tolerating it is
        deliberate; `UnavailableApp` says a named app is suspect rather than
        settled, and the counter below is what makes the rate visible.
        """
        empty_user_table = LifecycleHook(
            name="crm",
            command=(
                "echo 'restored 0 of 4812 users' >&2; "
                f'echo "{_REJECTION}" >&2; exit 1'
            ),
            env={"EXPERT_MODAL_ACTOR_EMAIL": _ACTOR},
        )
        _, unavailable = await run_post_populate_hooks([empty_user_table])
        assert [u.name for u in unavailable] == ["crm"]

    async def test_a_tolerated_app_is_counted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A degraded run has to be alertable on a RATE, not grepped for."""
        points = _capture_unavailable_metric(monkeypatch)
        await run_post_populate_hooks([_rejecting_hook("crm"), _ok_hook("email")])
        assert points == [(_UNAVAILABLE_METRIC, 1.0, ["app:crm"])]

    async def test_every_lost_app_gets_its_own_point(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tagged per app, so two apps in one second cannot collapse into one."""
        points = _capture_unavailable_metric(monkeypatch)
        await run_post_populate_hooks([_rejecting_hook("crm"), _rejecting_hook("mail")])
        assert sorted(p[2][0] for p in points if p[2]) == ["app:crm", "app:mail"]

    async def test_a_clean_run_counts_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        points = _capture_unavailable_metric(monkeypatch)
        await run_post_populate_hooks([_ok_hook("crm")])
        assert points == []

    async def test_a_hook_that_lost_the_persona_says_so_and_still_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sandbox has a persona, the hook does not: shout, then fail.

        `with_studio_env` is what puts the actor in `hook.env`; if that merge is
        ever lost the hooks run as nobody and every failure becomes
        unclassifiable — which looks exactly like a real failure. The carrier is
        the only evidence the two disagree, so it is logged rather than read as
        the actor.
        """
        monkeypatch.delenv("EXPERT_MODAL_ACTOR_EMAIL", raising=False)
        monkeypatch.setenv("MERCOR_STUDIO_ENV__EXPERT_MODAL_ACTOR_EMAIL", _ACTOR)
        unpinned = LifecycleHook(name="crm", command=f'echo "{_REJECTION}" >&2; exit 1')

        logged: list[str] = []
        sink_id = logger.add(lambda m: logged.append(str(m)), level="ERROR")
        try:
            with pytest.raises(RuntimeError, match="exit code 1"):
                await run_post_populate_hooks([unpinned])
        finally:
            logger.remove(sink_id)

        assert any(
            "running as NO pinned actor" in line
            and "MERCOR_STUDIO_ENV__EXPERT_MODAL_ACTOR_EMAIL" in line
            for line in logged
        )

    async def test_a_world_with_no_studio_env_logs_no_lost_merge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every task today carries no studio env; none of them may log this."""
        monkeypatch.delenv("EXPERT_MODAL_ACTOR_EMAIL", raising=False)
        monkeypatch.delenv("MERCOR_STUDIO_ENV__EXPERT_MODAL_ACTOR_EMAIL", raising=False)
        logged: list[str] = []
        sink_id = logger.add(lambda m: logged.append(str(m)), level="ERROR")
        try:
            with pytest.raises(RuntimeError, match="table dump corrupt"):
                await run_post_populate_hooks([_broken_hook()])
        finally:
            logger.remove(sink_id)

        assert not any("NO pinned actor" in line for line in logged)


class TestTheCallerDecides:
    """`handle_populate` only degrades when the caller asked for it.

    The default is the safety property: a caller that will not carry
    `unavailable_apps` out to the trajectory must keep failing closed, or a run
    that went without one of its apps saves looking identical to a clean one.
    """

    @pytest.mark.asyncio
    async def test_opted_out_caller_still_fails_on_a_rejection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No flag — today's behaviour. This is the Harbor path."""

        async def _fake_populate_data(**_kwargs: object) -> PopulateResult:
            return PopulateResult(objects_added=1)

        monkeypatch.setattr(populate_main, "populate_data", _fake_populate_data)
        # The 500 from the RCA: the endpoint turns a hook failure into one, and
        # that is what takes the whole provision down.
        with pytest.raises(HTTPException) as caught:
            await populate_main.handle_populate(
                PopulateRequest(
                    sources=[_A_SOURCE],
                    post_populate_hooks=[_rejecting_hook()],
                )
            )
        assert caught.value.status_code == 500

    @pytest.mark.asyncio
    async def test_opted_in_caller_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_populate_data(**_kwargs: object) -> PopulateResult:
            return PopulateResult(objects_added=1)

        monkeypatch.setattr(populate_main, "populate_data", _fake_populate_data)
        result = await populate_main.handle_populate(
            PopulateRequest(
                sources=[_A_SOURCE],
                post_populate_hooks=[_rejecting_hook()],
                tolerate_identity_rejection=True,
            )
        )
        assert [a.name for a in result.unavailable_apps] == ["deeptune_app"]

    @pytest.mark.asyncio
    async def test_opted_in_caller_still_fails_on_a_real_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asking to tolerate identity rejections is not asking to tolerate bugs."""

        async def _fake_populate_data(**_kwargs: object) -> PopulateResult:
            return PopulateResult(objects_added=1)

        monkeypatch.setattr(populate_main, "populate_data", _fake_populate_data)
        with pytest.raises(HTTPException) as caught:
            await populate_main.handle_populate(
                PopulateRequest(
                    sources=[_A_SOURCE],
                    post_populate_hooks=[_broken_hook()],
                    tolerate_identity_rejection=True,
                )
            )
        assert caught.value.status_code == 500

    def test_the_default_is_off(self) -> None:
        assert (
            PopulateRequest(
                sources=[_A_SOURCE], post_populate_hooks=[_ok_hook("crm")]
            ).tolerate_identity_rejection
            is False
        )


class TestSnapshotPathUnchanged:
    """`run_lifecycle_hooks` keeps failing closed — pre-snapshot hooks use it.

    A service that cannot quiesce must not yield a snapshot that looks
    complete, so the tolerance must NOT have leaked into the shared helper.
    """

    async def test_identity_rejection_still_raises_there(self) -> None:
        with pytest.raises(RuntimeError, match="401"):
            await run_lifecycle_hooks([_ok_hook("crm"), _rejecting_hook()])

    async def test_single_hook_failure_still_raises(self) -> None:
        with pytest.raises(RuntimeError, match="table dump corrupt"):
            await run_lifecycle_hooks([_broken_hook()])

    async def test_success_still_returns_timings(self) -> None:
        timings = await run_lifecycle_hooks([_ok_hook("a"), _ok_hook("b")])
        assert [t.name for t in timings] == ["a", "b"]
