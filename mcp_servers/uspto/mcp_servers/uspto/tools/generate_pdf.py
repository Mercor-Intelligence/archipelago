"""Patent PDF generation tool for the USPTO MCP server."""

from __future__ import annotations

import base64
from typing import Annotated

from loguru import logger
from pydantic import Field

from mcp_servers.uspto.api import get_uspto_client
from mcp_servers.uspto.auth.rate_limiter import rate_limiter
from mcp_servers.uspto.models import GeneratePatentPdfRequest, GeneratePatentPdfResponse
from mcp_servers.uspto.utils.errors import (
    InvalidRequestError,
    NotFoundError,
    RateLimitError,
    USPTOError,
    handle_errors,
)


@handle_errors
async def uspto_patent_pdf_generate(
    application_number: Annotated[
        str,
        Field(
            pattern=r"^(\d{2}/\d{3},\d{3}|\d{6,})$",
            description="Application number in formatted or digits-only form.",
        ),
    ],
) -> GeneratePatentPdfResponse:
    """Generate a text-only PDF of a patent from local database content.

    Creates a PDF containing the patent's title, abstract, description, and claims.
    Returns base64-encoded PDF bytes for direct use or file saving.

    IMPORTANT: This generates a TEXT-ONLY PDF without images, drawings, or original
    formatting. For official USPTO PDFs with full content, use document download URLs.

    APPLICATION NUMBER FORMAT: Accepts both formatted ('16/123,456') and
    digits-only ('16123456') forms.

    RESPONSE: Contains pdf_bytes (base64-encoded), file_name, byte_size, and
    a note indicating this is text-only content.

    COMMON ERRORS:
    - NOT_FOUND: Application does not exist in the database
    - INVALID_APPLICATION_NUMBER: Malformed application number
    - PDF_GENERATION_FAILED: Internal error generating the PDF
    - RATE_LIMIT_EXCEEDED: Too many requests (export: 20/min)
    """
    request = GeneratePatentPdfRequest(application_number=application_number)
    rate_limit = rate_limiter.check_rate_limit("export")
    if not rate_limit.allowed:
        raise RateLimitError(
            limit=rate_limit.limit,
            retry_after=rate_limit.retry_after,
            reset_at=rate_limit.reset_at,
        )

    client = get_uspto_client()

    try:
        logger.info(
            "Generating patent PDF",
            application_number=request.application_number,
        )

        result = await client.generate_patent_pdf(request.application_number)
    finally:
        await client.aclose()

    if "error" in result:
        error_info = result["error"]
        error_code = error_info.get("code", "UPSTREAM_ERROR")

        if error_code == "APPLICATION_NOT_FOUND":
            raise NotFoundError("application", request.application_number)

        if error_code == "INVALID_APPLICATION_NUMBER":
            raise InvalidRequestError(
                message="Application number is invalid.",
                details={"applicationNumber": request.application_number},
            )

        if error_code == "PDF_GENERATION_FAILED":
            raise USPTOError(
                code=error_code,
                message=error_info.get("message", "Failed to generate patent PDF"),
                details=error_info.get("details", {}),
                status_code=500,
            )

        raise USPTOError(
            code=error_code,
            message=error_info.get("message", "Failed to generate patent PDF"),
            details=error_info.get("details", {}),
            status_code=503,
        )

    encoded_pdf = base64.b64encode(result["pdfBytes"]).decode("ascii")
    return GeneratePatentPdfResponse(
        application_number=result.get("applicationNumber") or request.application_number,
        generated_at=result["generatedAt"],
        content_type=result["contentType"],
        file_name=result["fileName"],
        text_only=result["textOnly"],
        byte_size=result["byteSize"],
        note=result.get("note"),
        pdf_bytes=encoded_pdf,
    )


__all__ = ["uspto_patent_pdf_generate"]
