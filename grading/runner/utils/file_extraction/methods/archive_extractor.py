"""Extraction for bounded zip archives."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import re
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Protocol

from loguru import logger

from ..base import BaseFileExtractor
from ..types import ExtractedContent, ImageMetadata


class ExtractionService(Protocol):
    """Subset of the extraction service needed for archive members."""

    def can_extract_text(self, file_path: Path) -> bool: ...

    async def extract_from_file(
        self, file_path: Path, *, include_images: bool = True
    ) -> ExtractedContent | None: ...


MAX_ARCHIVE_MEMBERS = 60
MAX_ARCHIVE_MEMBER_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 250 * 1024 * 1024
MAX_ARCHIVE_TEXT_CHARS = 1_500_000
MAX_ARCHIVE_IMAGES = 10
# Same per-image ceiling the top-level pure-image path uses; with
# MAX_ARCHIVE_IMAGES that also matches its 50 MiB total payload ceiling.
MAX_ARCHIVE_IMAGE_BYTES = 5 * 1024 * 1024
# Members needing a document backend (pdf/docx/xlsx/...) each cost a Reducto
# parse, so one archive cannot fan out into dozens of them.
MAX_ARCHIVE_DOCUMENT_MEMBERS = 10
# Mirrors PURE_IMAGE_EXTENSIONS: only formats the multimodal provider accepts,
# so an archive cannot smuggle e.g. a gif into a judge request.
IMAGE_EXTENSIONS = {".jpeg", ".jpg", ".png", ".webp"}


class ArchiveExtractor(BaseFileExtractor):
    """Extract supported files from zip archives without unbounded expansion."""

    def __init__(self, extraction_service: ExtractionService):
        self._extraction_service = extraction_service

    @property
    def name(self) -> str:
        return "zip"

    def supports_file_type(self, file_extension: str) -> bool:
        return file_extension.lower() == ".zip"

    async def extract_from_file(
        self,
        file_path: Path,
        *,
        include_images: bool = True,
        sub_artifact_index: int | None = None,
    ) -> ExtractedContent | None:
        del sub_artifact_index
        try:
            archive_size = file_path.stat().st_size
            if archive_size > MAX_ARCHIVE_FILE_BYTES:
                logger.warning(
                    f"[ZIP] Skipping {file_path.name}: archive size "
                    f"{archive_size} exceeds {MAX_ARCHIVE_FILE_BYTES} byte cap"
                )
                return None
            archive = await asyncio.to_thread(zipfile.ZipFile, file_path)
            try:
                return await self._extract_archive(
                    archive, include_images=include_images
                )
            finally:
                await asyncio.shield(asyncio.to_thread(archive.close))
        except (zipfile.BadZipFile, RuntimeError, zipfile.LargeZipFile) as exc:
            logger.warning(f"[ZIP] Could not extract {file_path.name}: {exc}")
            return None
        except Exception as exc:
            logger.warning(
                f"[ZIP] Unexpected extraction failure for {file_path.name}: {exc}"
            )
            return None

    async def _extract_archive(
        self, archive: zipfile.ZipFile, *, include_images: bool
    ) -> ExtractedContent | None:
        infos = await asyncio.to_thread(self._eligible_members, archive)

        text_parts: list[str] = []
        images: list[ImageMetadata] = []
        omitted: dict[str, int] = {}
        truncated: dict[str, int] = {}
        extracted_members = 0
        total_bytes = 0
        document_members = 0
        text_chars = 0
        image_seq = 0
        has_content = False

        for index, info in enumerate(infos):
            if index >= MAX_ARCHIVE_MEMBERS:
                self._omit(omitted, "member cap")
                continue

            if info.flag_bits & 0x1:
                self._omit(omitted, "encrypted member")
                continue

            member_name = info.filename
            if self._is_hidden_member(member_name):
                self._omit(omitted, "hidden member")
                continue

            if Path(member_name).suffix.lower() == ".zip":
                self._omit(omitted, "nested zip depth limit")
                continue

            # Declared sizes are attacker-controlled, so they only buy us a
            # cheap skip; the real bound is the capped read below.
            if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                self._omit(omitted, "member size cap")
                continue
            if total_bytes + info.file_size > MAX_ARCHIVE_BYTES:
                self._omit(omitted, "total size cap")
                continue

            support = self._member_support(member_name)
            if support is None:
                self._omit(omitted, "unsupported member")
                continue

            needs_document_backend = support == "document"
            if needs_document_backend and document_members >= (
                MAX_ARCHIVE_DOCUMENT_MEMBERS
            ):
                self._omit(omitted, "document extraction cap")
                continue

            try:
                member_bytes = await self._read_member(
                    archive, info, limit=MAX_ARCHIVE_MEMBER_BYTES
                )
                if len(member_bytes) > MAX_ARCHIVE_MEMBER_BYTES:
                    self._omit(omitted, "member size cap")
                    continue
                if total_bytes + len(member_bytes) > MAX_ARCHIVE_BYTES:
                    self._omit(omitted, "total size cap")
                    continue
                total_bytes += len(member_bytes)
                if needs_document_backend:
                    document_members += 1
                text, member_images = await self._extract_member(
                    member_name,
                    member_bytes,
                    include_images=include_images,
                )
            except zipfile.BadZipFile as exc:
                logger.warning(
                    f"[ZIP] Could not extract corrupt member {member_name!r}: {exc}"
                )
                self._omit(omitted, "corrupt member")
                continue
            except Exception as exc:
                logger.warning(f"[ZIP] Could not extract member {member_name!r}: {exc}")
                self._omit(omitted, "no extractable content")
                continue

            if not text and not member_images:
                self._omit(omitted, "no extractable content")
                continue
            has_content = True
            text, member_images = self._renumber_images(text, member_images, image_seq)
            image_seq += len(member_images)

            if member_images and not text:
                text_parts.append(f"--- {member_name} ---")

            remaining_text = MAX_ARCHIVE_TEXT_CHARS - text_chars
            if text and remaining_text > 0:
                kept_text = text[:remaining_text]
                text_chars += len(kept_text)
                text_parts.append(f"--- {member_name} ---\n{kept_text}")
                if len(kept_text) < len(text):
                    self._omit(truncated, "text cap")
            elif text:
                self._omit(truncated, "text cap")

            remaining_images = MAX_ARCHIVE_IMAGES - len(images)
            if include_images and remaining_images > 0:
                images.extend(member_images[:remaining_images])
                if len(member_images) > remaining_images:
                    self._omit(truncated, "image cap")
            elif member_images:
                self._omit(truncated, "image cap")
            extracted_members += 1

        if not has_content:
            logger.warning(
                f"[ZIP] No extractable content found in archive with {len(infos)} members"
            )
            return None

        notices: list[str] = []
        if omitted:
            reasons = ", ".join(
                f"{count} {reason}" for reason, count in omitted.items()
            )
            notices.append(
                f"{sum(omitted.values())} of {len(infos)} members omitted: {reasons}"
            )
        if truncated:
            reasons = ", ".join(
                f"{count} {reason}" for reason, count in truncated.items()
            )
            notices.append(f"content truncated: {reasons}")
        if notices:
            text_parts.append(f"[{'; '.join(notices)}]")

        return ExtractedContent(
            text="\n\n".join(text_parts),
            images=images,
            extraction_method="zip",
            metadata={
                "file_type": ".zip",
                "member_count": len(infos),
                "extracted_member_count": extracted_members,
                "omitted_count": sum(omitted.values()),
                "omitted_reasons": omitted,
                "truncated_reasons": truncated,
                "extracted_char_count": text_chars,
                "image_count": len(images),
            },
        )

    async def _extract_member(
        self, member_name: str, member_bytes: bytes, *, include_images: bool
    ) -> tuple[str, list[ImageMetadata]]:
        suffix = Path(member_name).suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            if not include_images:
                return "", []
            return await self._extract_image_member(member_name, member_bytes)

        if not self._extraction_service.can_extract_text(Path(member_name)):
            return "", []

        spilled: list[Path] = []
        cleanup = threading.Event()
        spill = asyncio.ensure_future(
            asyncio.to_thread(
                self._spill_to_disk, member_bytes, suffix, spilled, cleanup
            )
        )
        try:
            await asyncio.shield(spill)
            extracted = await self._extraction_service.extract_from_file(
                spilled[0], include_images=include_images
            )
            if extracted is None:
                return "", []
            return extracted.text or "", list(extracted.images or [])
        finally:
            cleanup.set()
            await asyncio.shield(asyncio.to_thread(self._unlink_all, spilled))

    @staticmethod
    async def _extract_image_member(
        member_name: str, member_bytes: bytes
    ) -> tuple[str, list[ImageMetadata]]:
        """Same validation the top-level pure-image path applies: real image
        bytes, a known image MIME type, under the per-image ceiling."""
        # import-check-ignore
        from runner.helpers.snapshot_diff.constants import (
            PURE_IMAGE_MIME_TYPES,
            has_image_magic_bytes,
        )

        if len(member_bytes) > MAX_ARCHIVE_IMAGE_BYTES:
            logger.warning(
                f"[ZIP] Skipping image {member_name!r}: {len(member_bytes)} bytes "
                f"exceeds {MAX_ARCHIVE_IMAGE_BYTES} byte cap"
            )
            return "", []
        if not has_image_magic_bytes(member_bytes):
            logger.warning(f"[ZIP] Skipping {member_name!r}: not image bytes")
            return "", []

        suffix = Path(member_name).suffix.lower()
        mime_type = (
            PURE_IMAGE_MIME_TYPES.get(suffix) or mimetypes.guess_type(member_name)[0]
        )
        if not mime_type or not mime_type.startswith("image/"):
            logger.warning(f"[ZIP] Skipping image {member_name!r}: unknown MIME type")
            return "", []

        encoded = await asyncio.to_thread(base64.b64encode, member_bytes)
        image = ImageMetadata(
            url=f"data:{mime_type};base64,{encoded.decode('ascii')}",
            placeholder="[IMAGE_1]",
            type="Figure",
            caption=member_name,
        )
        return image.placeholder, [image]

    def _member_support(self, member_name: str) -> str | None:
        """How this member would be read: as an image, plain text, or a document
        parse (which costs a Reducto call). `None` means nothing can read it, so
        it must not spend any budget."""
        # import-check-ignore
        from runner.helpers.snapshot_diff.constants import TEXT_EXTENSIONS

        suffix = Path(member_name).suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            return "image"
        if not self._extraction_service.can_extract_text(Path(member_name)):
            return None
        return "text" if suffix in TEXT_EXTENSIONS else "document"

    @staticmethod
    def _renumber_images(
        text: str, images: list[ImageMetadata], start: int
    ) -> tuple[str, list[ImageMetadata]]:
        """Give each image an archive-unique placeholder, keeping text in sync."""
        renumbered: list[ImageMetadata] = []
        mapping: dict[str, str] = {}
        for offset, image in enumerate(images):
            placeholder = f"[IMAGE_{start + offset + 1}]"
            if image.placeholder:
                mapping.setdefault(image.placeholder, placeholder)
            renumbered.append(image.model_copy(update={"placeholder": placeholder}))
        if mapping:
            pattern = "|".join(re.escape(old) for old in mapping)
            text = re.sub(pattern, lambda match: mapping[match.group(0)], text)
        return text, renumbered

    @staticmethod
    def _unlink_all(spilled: list[Path]) -> None:
        for member_path in spilled:
            member_path.unlink(missing_ok=True)

    @staticmethod
    def _spill_to_disk(
        member_bytes: bytes,
        suffix: str,
        spilled: list[Path],
        cleanup: threading.Event,
    ) -> None:
        """Write a member to a temp file the caller can read back.

        Cleanup ownership never depends on the event loop: the worker deletes the
        file itself if ``cleanup`` is already set when the write ends, so a
        cancelled or shut-down caller cannot orphan it.
        """
        handle, name = tempfile.mkstemp(suffix=suffix)
        member_path = Path(name)
        spilled.append(member_path)
        try:
            try:
                member_file = os.fdopen(handle, "wb")
            except BaseException:
                os.close(handle)
                raise
            with member_file:
                member_file.write(member_bytes)
        finally:
            if cleanup.is_set():
                member_path.unlink(missing_ok=True)

    @classmethod
    async def _read_member(
        cls,
        archive: zipfile.ZipFile,
        info: zipfile.ZipInfo,
        *,
        limit: int = MAX_ARCHIVE_MEMBER_BYTES,
    ) -> bytes:
        """Read at most ``limit`` + 1 decompressed bytes, so a member that inflates
        past the cap is detectable without ever materializing it.

        Let the worker finish before unwinding: the caller closes the archive on
        the way out, which would break a read still running in the thread.
        """
        read = asyncio.ensure_future(
            asyncio.to_thread(cls._read_member_sync, archive, info, limit)
        )
        try:
            return await asyncio.shield(read)
        except asyncio.CancelledError:
            await asyncio.gather(read, return_exceptions=True)
            raise

    @staticmethod
    def _read_member_sync(
        archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int
    ) -> bytes:
        with archive.open(info) as member_file:
            return member_file.read(limit + 1)

    @classmethod
    def _eligible_members(cls, archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        return [info for info in archive.infolist() if not info.is_dir()]

    @staticmethod
    def _is_hidden_member(member_name: str) -> bool:
        return any(
            component == "__MACOSX" or component.startswith(".")
            for component in Path(member_name).parts
        )

    @staticmethod
    def _omit(omitted: dict[str, int], reason: str) -> None:
        omitted[reason] = omitted.get(reason, 0) + 1
