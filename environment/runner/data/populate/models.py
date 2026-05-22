"""Pydantic models for populate operations.

This module defines request and response models for the populate endpoint,
including validation logic for subsystem names and S3 URLs.
"""

import os

from pydantic import BaseModel, Field, field_validator, model_validator

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

    @model_validator(mode="after")
    def validate_has_work(self) -> "PopulateRequest":
        """Validate that there is something to do.

        Either sources or hooks must be provided, otherwise the request is a no-op.
        """
        if not self.sources and not self.post_populate_hooks:
            raise ValueError("At least one source or hook must be provided")
        return self


class PopulateResult(BaseModel):
    """Result of S3 populate operation.

    Returned by the /data/populate/s3 endpoint after successfully downloading
    and placing objects from S3 into subsystem directories.
    """

    objects_added: int = Field(
        ...,
        description="Total number of objects (files) downloaded and added to subsystems",
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
