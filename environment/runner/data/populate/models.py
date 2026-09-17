"""Pydantic models for populate operations.

This module defines request and response models for the populate endpoint,
including validation logic for subsystem names and S3 URLs.
"""

import os
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ...utils.s3 import S3Credentials
from ...utils.settings import get_settings

settings = get_settings()


class PopulateSource(BaseModel):
    """Single S3 source with subsystem mapping.

    Represents a single S3 location (object or prefix) to download and the
    subsystem directory where it should be placed. The subsystem must start
    with 'filesystem' or '.apps_data' to ensure it's covered by snapshots.
    """

    url: str = Field(
        ...,
        description=(
            "S3 URL in format 's3://bucket/key'. Can point to a single object or a prefix (directory)."
        ),
    )
    subsystem: str = Field(
        default="filesystem",
        description=(
            "Subsystem name where files will be placed. Must be 'filesystem', '.apps_data', or a nested path under one of these (e.g., 'filesystem/data', '.apps_data/custom'). Defaults to 'filesystem'."
        ),
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate that the S3 URL is not empty.

        Strips whitespace from the URL and ensures it contains at least
        one non-whitespace character.

        Args:
            v: The URL string to validate

        Returns:
            The stripped URL string

        Raises:
            ValueError: If the URL is empty or contains only whitespace
        """
        if not v or not v.strip():
            raise ValueError("URL cannot be empty")
        return v.strip()

    @field_validator("subsystem")
    @classmethod
    def validate_subsystem(cls, v: str) -> str:
        """Validate subsystem name is safe and starts with allowed root subsystem.

        Subsystems must start with 'filesystem' or '.apps_data' to ensure they are
        covered by snapshots. Allows nested paths like '.apps_data/custom' or
        'filesystem/data' but prevents:
        - Path traversal with '..'
        - Windows path separators '\\'
        - Starting with '/' (we prepend '/' in code)
        - Subsystems outside the allowed roots
        """
        if not v or not v.strip():
            raise ValueError("Subsystem name cannot be empty")
        v = v.strip()

        # Prevent starting with / (we prepend it in code)
        if v.startswith("/"):
            raise ValueError("Subsystem name cannot start with '/'")

        # Prevent path traversal
        if ".." in v:
            raise ValueError(
                "Subsystem name cannot contain '..' (path traversal not allowed)"
            )

        # Prevent Windows path separators
        if "\\" in v:
            raise ValueError(
                "Subsystem name cannot contain '\\' (use '/' for nested paths)"
            )

        # Normalize and check for unresolved path traversal
        # After normalization, if ".." remains, it means there are too many
        # parent directory references that could escape the root
        normalized = os.path.normpath(v)
        if ".." in normalized:
            raise ValueError(f"Invalid subsystem path (unresolved path traversal): {v}")

        # Enforce that subsystem must start with allowed root subsystems
        # Use settings constants to ensure consistency
        # Check if subsystem is exactly the root or a nested path under it
        is_valid = (
            v == settings.FILESYSTEM_SUBSYSTEM_NAME
            or v.startswith(f"{settings.FILESYSTEM_SUBSYSTEM_NAME}/")
            or v == settings.APPS_DATA_SUBSYSTEM_NAME
            or v.startswith(f"{settings.APPS_DATA_SUBSYSTEM_NAME}/")
        )

        if not is_valid:
            examples = f"'{settings.FILESYSTEM_SUBSYSTEM_NAME}/data' or '{settings.APPS_DATA_SUBSYSTEM_NAME}/custom'"
            msg = (
                f"Subsystem must be '{settings.FILESYSTEM_SUBSYSTEM_NAME}', '{settings.APPS_DATA_SUBSYSTEM_NAME}', "
                f"or a nested path under one of these roots (e.g., {examples})"
            )
            raise ValueError(msg)

        return v


class LifecycleHook(BaseModel):
    """A shell command to run at a specific lifecycle point.

    Used for post-populate hooks that run after data is extracted.
    """

    name: str = Field(..., description="Service name (for logging)")
    command: str = Field(..., description="Shell command to execute")
    env: dict[str, str] | None = Field(
        default=None,
        description="Environment variables for the command.",
    )
    timeout_seconds: float | None = Field(
        default=None,
        description=(
            "Per-hook execution timeout. Falls back to the runner's "
            "LIFECYCLE_HOOK_TIMEOUT_SECONDS env var (default 3000) when unset."
        ),
    )


class HookTiming(BaseModel):
    """Timing data for a single lifecycle hook execution."""

    name: str = Field(..., description="Service name the hook ran for")
    duration_s: float = Field(..., description="Hook execution duration in seconds")


class PopulateRequest(BaseModel):
    """Request to populate subsystems from S3 sources.

    Contains a list of S3 sources, each mapping to a subsystem directory.
    Sources are processed in order, with later sources overwriting earlier
    ones if they have the same destination path.

    Optionally includes post-populate hooks that run after data extraction.
    """

    sources: list[PopulateSource] = Field(
        ...,
        description=(
            "List of S3 sources to download. Each source specifies an S3 URL and the subsystem where it should be placed."
        ),
    )
    post_populate_hooks: list[LifecycleHook] = Field(
        default_factory=list,
        description="Commands to run after data extraction (e.g., load database dumps).",
    )
    s3_credentials: S3Credentials | None = Field(
        default=None,
        description="Optional credentials to use for the populate operation.",
    )
    s3_transfer_backend: str = Field(
        default="boto3",
        description=(
            "S3 download backend to use: 'boto3' (default) or 's5cmd'. Resolved "
            "server-side from the TRAJECTORY_S3_TRANSFER_BACKEND PostHog flag. "
            "Unknown values and a missing s5cmd binary fall back to boto3."
        ),
    )

    tolerate_identity_rejection: bool = Field(
        default=False,
        description=(
            "Allow a hook that fails because the app says the pinned actor does "
            "not exist to mark that app unavailable instead of failing the whole "
            "populate. OFF BY DEFAULT, and the default is the safety property: "
            "only a caller that carries `unavailable_apps` out to the trajectory "
            "may ask for it, because a caller that cannot record the degradation "
            "would save a run that ran without an app looking identical to a "
            "clean one. An older runner ignores this field and fails closed, "
            "which is its current behaviour."
        ),
    )

    @model_validator(mode="after")
    def validate_has_work(self) -> "PopulateRequest":
        """Validate that there is something to do.

        Either sources or hooks must be provided, otherwise the request is a no-op.
        """
        if not self.sources and not self.post_populate_hooks:
            raise ValueError("At least one source or hook must be provided")
        return self


class UnavailableApp(BaseModel):
    """A service whose post-populate hook refused the pinned runtime actor.

    NOT A VERDICT, AND NOT A PERMISSION BOUNDARY. This records only that the app
    said the pinned actor does not exist. It does NOT establish why, and the two
    causes are opposites:

    * the persona genuinely has no account there — a sales persona has no GitHub,
      and an absent app is the honest world;
    * the seed pipeline failed to map the persona in — the persona SHOULD have
      had access and the app is broken.

    INCLUDING THE CASE WHERE THE APP SEEDED NOBODY AT ALL. A truncated users
    CSV, a dump restore that fails but still exits 0, or a seed-ordering bug
    leaves the user table EMPTY, and an empty user table refuses the actor with
    the byte-identical line it uses for a persona who was simply never meant to
    be there — the app's auth layer answers "no user found with email: …" either
    way. Nothing in the runner can separate them: the only process that knows
    whether it wrote one row or none is the seed loader, and it has already
    exited. So this list also collects apps that are not app-less but BROKEN.

    Nothing here can tell those apart, because Studio has no per-app declaration
    of who this persona is supposed to be. So a NON-EMPTY LIST IS SUSPECT rather
    than settled: until the person-with-alias catalog can say which apps a
    persona was meant to reach, a run that names one should be treated as
    quarantined, not as a legitimately app-less world. The second cause is the
    common one today, and ``studio.trajectory.populate_app_unavailable`` is
    tagged per app so the rate is watchable rather than only recorded.

    Tolerating it at all is still right. The alternative shipped for a long time
    and was much worse: the hook exited non-zero, the populate endpoint 500d, and
    the provision died for EVERY OTHER app on the platform, so no trajectory
    could start at all.

    NAMING AN APP HERE DOES NOT WITHDRAW ITS TOOLS. The service still runs and
    ``/apps`` still registers its MCP server, so the agent can discover and call
    tools that will fail for the same reason the seed did. Recording the app is
    what makes that diagnosable. Hiding the tools would be the honest
    environment, and is deliberately not what this does yet.

    Carried on ``PopulateResult`` rather than only logged because "which apps
    did this run not have" has to be answerable later, per trajectory, without
    re-reading container logs — a run that silently dropped an app is exactly
    the thing that must not look identical to a clean one.
    """

    name: str = Field(
        ...,
        description="Service name whose hook could not authenticate the actor",
    )
    reason: str = Field(
        ...,
        description="Operator-facing reason, taken from the hook's own stderr",
    )


class PopulateResult(BaseModel):
    """Result of S3 populate operation.

    Returned by the /data/populate/s3 endpoint after successfully downloading
    and placing objects from S3 into subsystem directories.
    """

    objects_added: int = Field(
        ...,
        description="Total number of objects (files) downloaded and added to subsystems",
    )
    download_seconds: float | None = Field(
        default=None,
        description=(
            "Wall-clock seconds spent fetching objects from S3, across every "
            "source. None from a platform image built before this field existed "
            "— the caller must treat missing as unknown, not as zero."
        ),
    )
    hook_timings: list[HookTiming] = Field(
        default_factory=list,
        description="Per-hook execution timing for post-populate hooks",
    )
    unavailable_apps: list[UnavailableApp] = Field(
        default_factory=list,
        description=(
            "Services skipped because they could not authenticate the pinned "
            "runtime actor. Empty on a populate where every hook succeeded."
        ),
    )


class PopulateJobStarted(BaseModel):
    """Response for ``POST /data/populate/s3/start``.

    Carries the id the caller polls via ``/data/populate/s3/status/{job_id}``.
    """

    job_id: str = Field(
        ...,
        description="Opaque id to poll for this async populate's status",
    )


class PopulateJobStatus(BaseModel):
    """Response for ``GET /data/populate/s3/status/{job_id}``."""

    status: Literal["running", "done", "error"] = Field(
        ..., description="Current state of the background populate job"
    )
    result: PopulateResult | None = Field(
        default=None, description="Populate result, set once status == 'done'"
    )
    error: str | None = Field(
        default=None, description="Failure detail, set once status == 'error'"
    )


class PopulateStreamResult(BaseModel):
    """Result of direct upload populate operation.

    Returned by the /data/populate endpoint after successfully extracting
    a tar.gz archive into a subsystem directory.
    """

    objects_added: int = Field(
        ...,
        description="Total number of objects (files) extracted from the archive",
    )
    subsystem: str = Field(
        ...,
        description="Target subsystem where files were extracted",
    )
    extracted_bytes: int = Field(
        ...,
        description="Total size of extracted files in bytes",
    )
    hook_timings: list[HookTiming] = Field(
        default_factory=list,
        description="Per-hook execution timing for post-populate hooks",
    )
