"""
Factory for creating file extractors.
"""

import os
from pathlib import Path

from loguru import logger

from .base import BaseFileExtractor
from .methods import (
    LocalExtractor,
    MercorDocumentCache,
    ReductoExtractor,
    mercor_document_cache_env_configured,
)
from .types import ExtractedContent


class FileExtractionService:
    """
    Service for extracting content from files using various methods.

    This service automatically selects the best available extraction method
    for each file type.
    """

    def __init__(self):
        """Initialize the file extraction service with available extractors"""
        self._extractors: list[BaseFileExtractor] = []
        self._initialize_extractors()

    def _initialize_extractors(self):
        """Initialize all available extractors"""
        # Initialize local extractor first (fast, for change detection)
        try:
            local_extractor = LocalExtractor()
            # Only add if it supports at least one file type
            if local_extractor._supported_extensions:
                self._extractors.append(local_extractor)
                logger.info(
                    f"[FILE EXTRACTION SERVICE] Local extractor initialized for: {', '.join(sorted(local_extractor._supported_extensions))}"
                )
            else:
                logger.warning(
                    "[FILE EXTRACTION SERVICE] Local extractor has NO supported file types. "
                    "Install openpyxl, python-pptx, or python-docx for local extraction."
                )
        except Exception as e:
            logger.warning(
                f"[FILE EXTRACTION SERVICE] Could not initialize Local extractor: {e}"
            )

        # Initialize document extraction.
        #
        # Fallback order (used by `extract_from_file`):
        #   1. MercorDocumentCache — content-addressed, shares parses across
        #      snapshots. Requires MERCOR_DOCUMENT_API + MERCOR_DOCUMENT_API_KEY.
        #   2. ReductoExtractor — direct third-party parser. Bypasses our cache
        #      so we still grade when the cache backend is degraded. Requires
        #      REDUCTO_API_KEY.
        #   3. LocalExtractor — last-resort offline parser (registered above).
        #
        # Both MercorDocumentCache and the direct ReductoExtractor are
        # registered when their respective env vars are present; they are not
        # mutually exclusive any more.
        try:
            if mercor_document_cache_env_configured():
                self._extractors.append(MercorDocumentCache())
                logger.info(
                    "[FILE EXTRACTION SERVICE] MercorDocumentCache initialized "
                    "(primary: Mercor's document cache)"
                )
        except Exception as e:
            logger.warning(
                f"[FILE EXTRACTION SERVICE] Could not initialize MercorDocumentCache: {e}"
            )

        try:
            api_key = os.getenv("REDUCTO_API_KEY")
            if api_key:
                self._extractors.append(ReductoExtractor(api_key=api_key))
                logger.info(
                    "[FILE EXTRACTION SERVICE] ReductoExtractor initialized "
                    "(direct fallback — bypasses Mercor's document cache)"
                )
        except Exception as e:
            logger.warning(
                f"[FILE EXTRACTION SERVICE] Could not initialize ReductoExtractor: {e}"
            )

        if not any(isinstance(e, ReductoExtractor) for e in self._extractors):
            logger.warning(
                "[FILE EXTRACTION SERVICE] No document extraction backend "
                "configured. Set MERCOR_DOCUMENT_API + MERCOR_DOCUMENT_API_KEY "
                "and/or REDUCTO_API_KEY."
            )

        logger.info(
            f"[FILE EXTRACTION SERVICE] Initialization complete. "
            f"Available extractors: {[e.name for e in self._extractors]}"
        )

        # Future: Add more extractors here
        # try:
        #     from .methods import PyPDFExtractor
        #     self._extractors.append(PyPDFExtractor())
        # except Exception as e:
        #     logger.warning(f"Could not initialize PyPDF extractor: {e}")

    def _get_extractor_for_file(self, file_path: Path) -> BaseFileExtractor | None:
        """
        Get the best available extractor for a file type.

        Args:
            file_path: Path to the file

        Returns:
            An extractor that supports this file type, or None
        """
        file_extension = file_path.suffix.lower()

        # Return the first extractor that supports this file type
        for extractor in self._extractors:
            if extractor.supports_file_type(file_extension):
                return extractor

        return None

    def get_local_extractor(self, file_path: Path) -> BaseFileExtractor | None:
        """
        Get the local extractor for a file type (fast, for change detection).

        Args:
            file_path: Path to the file

        Returns:
            LocalExtractor if it supports this file type, None otherwise
        """
        file_extension = file_path.suffix.lower()
        for extractor in self._extractors:
            if isinstance(extractor, LocalExtractor) and extractor.supports_file_type(
                file_extension
            ):
                return extractor
        return None

    def get_reducto_extractor(self, file_path: Path) -> BaseFileExtractor | None:
        """
        Get the preferred Reducto-style document extractor for a file type.

        Returns the first registered ReductoExtractor (or subclass) that
        supports this file type. In production this is the Mercor document
        cache; in local dev or when only REDUCTO_API_KEY is configured this
        is the direct Reducto extractor.
        """
        file_extension = file_path.suffix.lower()
        for extractor in self._extractors:
            if isinstance(extractor, ReductoExtractor) and extractor.supports_file_type(
                file_extension
            ):
                return extractor
        return None

    def get_mercor_cache_extractor(self, file_path: Path) -> BaseFileExtractor | None:
        """Get the Mercor document cache extractor for this file type, if any."""
        file_extension = file_path.suffix.lower()
        for extractor in self._extractors:
            if isinstance(
                extractor, MercorDocumentCache
            ) and extractor.supports_file_type(file_extension):
                return extractor
        return None

    def get_direct_reducto_extractor(self, file_path: Path) -> BaseFileExtractor | None:
        """Get the direct (non-cached) ReductoExtractor for this file type, if any."""
        file_extension = file_path.suffix.lower()
        for extractor in self._extractors:
            if (
                isinstance(extractor, ReductoExtractor)
                and not isinstance(extractor, MercorDocumentCache)
                and extractor.supports_file_type(file_extension)
            ):
                return extractor
        return None

    async def extract_from_file(
        self,
        file_path: Path,
        *,
        include_images: bool = True,
        sub_artifact_index: int | None = None,
        prefer_reducto: bool = True,
    ) -> ExtractedContent | None:
        """
        Extract content from a file using the best available method.

        Automatically decides whether to:
        - Use a specialized extractor (PDF, DOCX, etc.)
        - Decode as plain text (TXT, PY, MD, etc.)
        - Return None (unsupported binary files)

        If the primary extractor fails, automatically falls back to the other extractor.

        Args:
            file_path: Path to the file
            include_images: Whether to extract images (only for document extractors)
            sub_artifact_index: Optional 0-based index of specific sub-artifact to extract
                              (only used for multi-part documents with Reducto)
            prefer_reducto: If True, try Reducto-style extractors first then
                           fall back to local. If False, try local first then
                           fall back to Reducto-style extractors.

        Returns:
            ExtractedContent if extraction succeeded, None otherwise
        """
        # Reducto-style extractors are tried in this order:
        #   1. Mercor document cache (content-addressed, shares parses).
        #   2. Direct Reducto (bypasses our cache; protects grading when the
        #      cache backend regresses).
        # Then the local extractor is the final offline fallback.
        mercor_cache = self.get_mercor_cache_extractor(file_path)
        direct_reducto = self.get_direct_reducto_extractor(file_path)
        local = self.get_local_extractor(file_path)

        if prefer_reducto:
            extractors = [mercor_cache, direct_reducto, local]
        else:
            extractors = [local, mercor_cache, direct_reducto]

        # Filter to only available extractors and de-duplicate while
        # preserving order (defensive — the getters above already partition
        # extractors, but this protects against future overlaps).
        seen: set[int] = set()
        deduped: list[BaseFileExtractor] = []
        for e in extractors:
            if e is None or id(e) in seen:
                continue
            seen.add(id(e))
            deduped.append(e)
        extractors = deduped

        last_error: Exception | None = None

        for extractor in extractors:
            try:
                if sub_artifact_index is not None:
                    logger.debug(
                        f"Using {extractor.name} to extract sub-artifact {sub_artifact_index} from {file_path.name}"
                    )
                else:
                    logger.debug(
                        f"Using {extractor.name} to extract content from {file_path.name}"
                    )

                result = await extractor.extract_from_file(
                    file_path,
                    include_images=include_images,
                    sub_artifact_index=sub_artifact_index,
                )
                if result is not None:
                    return result

            except Exception as e:
                last_error = e
                error_type = type(e).__name__
                error_msg = str(e)

                # Try to unwrap nested exceptions for better error messages
                if hasattr(e, "__cause__") and e.__cause__:
                    cause_type = type(e.__cause__).__name__
                    cause_msg = str(e.__cause__)
                    logger.warning(
                        f"Failed to extract content from {file_path.name} using {extractor.name}\n"
                        f"  Error: {error_type}: {error_msg}\n"
                        f"  Caused by: {cause_type}: {cause_msg}"
                    )
                else:
                    logger.warning(
                        f"Failed to extract content from {file_path.name} using {extractor.name}\n"
                        f"  Error: {error_type}: {error_msg}"
                    )

                # Try to extract HTTP status details if available
                if hasattr(e, "response"):
                    try:
                        response = getattr(e, "response", None)
                        if response is not None:
                            status_code = getattr(response, "status_code", None)
                            if status_code:
                                logger.warning(f"  HTTP Status: {status_code}")
                            response_text = getattr(response, "text", None)
                            if response_text:
                                logger.warning(f"  Response: {response_text[:500]}")
                    except Exception:
                        pass

                # If there are more extractors to try, continue
                if extractor != extractors[-1]:
                    logger.info(
                        f"[FALLBACK] Trying next extractor after {extractor.name} failed for {file_path.name}"
                    )
                    continue

        # All extractors failed
        if last_error is not None:
            logger.error(
                f"All extractors failed for {file_path.name}. Last error: {last_error}"
            )

        # Fallback: try plain text decoding for text files
        # import-check-ignore
        from runner.helpers.snapshot_diff.constants import (
            TEXT_EXTENSIONS,
        )

        if file_path.suffix.lower() in TEXT_EXTENSIONS:
            try:
                logger.debug(f"Decoding {file_path.name} as plain text")
                text = file_path.read_text(encoding="utf-8", errors="ignore")
                return ExtractedContent(
                    text=text,
                    images=[],
                    extraction_method="utf8_decode",
                    metadata={"file_type": file_path.suffix},
                )
            except Exception as e:
                logger.error(f"Failed to decode {file_path.name} as text: {e}")
                return None

        # No extraction method available
        logger.debug(f"No extraction method available for {file_path.suffix} files")
        return None

    def can_extract_text(self, file_path: Path) -> bool:
        """
        Check if we can extract text content from this file.

        This returns True for:
        - Plain text files (.txt, .py, .md, etc.) - will decode as UTF-8
        - Documents (.pdf, .docx, .pptx, .xlsx) - if extractor available

        Args:
            file_path: Path to the file (used to check extension)

        Returns:
            True if text content can be extracted from this file
        """
        suffix = file_path.suffix.lower()

        # Check if any extraction service supports this
        if any(extractor.supports_file_type(suffix) for extractor in self._extractors):
            return True

        # Check if it's a plain text file that can be UTF-8 decoded
        # import-check-ignore
        from runner.helpers.snapshot_diff.constants import (
            TEXT_EXTENSIONS,
        )

        return suffix in TEXT_EXTENSIONS

    @property
    def available_extractors(self) -> list[str]:
        """Get names of all available extractors"""
        return [extractor.name for extractor in self._extractors]
