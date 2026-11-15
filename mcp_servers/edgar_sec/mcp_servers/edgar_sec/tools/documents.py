"""Document retrieval tools for SEC filings.

Fetches actual document text from SEC EDGAR filings via edgartools,
or from local offline data when EDGAR_OFFLINE_MODE=true.
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import EDGAR_OFFLINE_MODE
from loguru import logger
from models import (
    FilingDocumentInfo,
    GetFilingDocumentInput,
    GetFilingDocumentOutput,
    ListFilingDocumentsInput,
    ListFilingDocumentsOutput,
)
from utils.cik_resolver import resolve_cik
from utils.xbrl_parser import get_filing_from_accession


def _is_edgartools_repr_error(text: str | bytes | None) -> bool:
    """Check if text is the edgartools __repr__ error message.

    edgartools returns this error string when Document.__repr__ returns empty.
    """
    if not text:
        return True
    if isinstance(text, bytes):
        return b".__repr__ returned empty string" in text
    return ".__repr__ returned empty string" in text


def _get_document_filename(doc) -> str:
    """Extract filename from edgartools document object."""
    filename = getattr(doc, "filename", None) or getattr(doc, "document", None)
    if not filename:
        doc_str = str(doc)
        if ".htm" in doc_str.lower():
            match = re.search(r"([a-zA-Z0-9_.-]+\.htm[l]?)", doc_str, re.IGNORECASE)
            filename = match.group(1) if match else doc_str
        else:
            filename = doc_str
    return filename


# =============================================================================
# Offline helpers
# =============================================================================


async def _list_filing_documents_offline(
    request: ListFilingDocumentsInput,
) -> ListFilingDocumentsOutput:
    """List filing documents using local offline data."""
    from repositories.factory import get_repository

    cik = await resolve_cik(request.cik, request.ticker, request.name)
    repo = get_repository()

    logger.info(f"[offline] Listing documents for filing {request.filing_accession}")

    doc_info = await repo.list_filing_documents(cik, request.filing_accession)

    # Build base URL (for reference, even in offline mode)
    cik_stripped = cik.lstrip("0")
    accession_clean = request.filing_accession.replace("-", "")
    base_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{accession_clean}"

    documents = []

    # Add primary document
    primary_doc = doc_info.get("primary_document")
    primary_desc = doc_info.get("primary_doc_description")
    if primary_doc:
        documents.append(
            FilingDocumentInfo(
                filename=primary_doc,
                description=primary_desc or "Primary document",
                url=f"{base_url}/{primary_doc}",
            )
        )

    # Add additional files from filings/ directory
    for filename in doc_info.get("additional_files", []):
        documents.append(
            FilingDocumentInfo(
                filename=filename,
                description=None,
                url=f"{base_url}/{filename}",
            )
        )

    return ListFilingDocumentsOutput(
        filing_accession=request.filing_accession,
        primary_document=primary_doc,
        documents=documents,
        total_documents=len(documents),
        data_source="offline_submissions",
    )


async def _get_filing_document_offline(
    request: GetFilingDocumentInput,
) -> GetFilingDocumentOutput:
    """Get filing document content using local offline data."""
    from repositories.factory import get_repository

    cik = await resolve_cik(request.cik, request.ticker, request.name)
    repo = get_repository()

    logger.info(
        f"[offline] Fetching document '{request.document}' from filing {request.filing_accession}"
    )

    # Resolve document name - for "primary", look up from submissions
    document_name = request.document
    if document_name == "primary":
        doc_info = await repo.list_filing_documents(cik, request.filing_accession)
        document_name = doc_info.get("primary_document")
        if not document_name:
            raise ValueError(
                f"No primary document found in submissions for filing "
                f"{request.filing_accession}. The submissions JSON may not include "
                f"primaryDocument for this filing."
            )

    # Try to read the filing HTML from local files
    text = await repo.get_filing_html(cik, request.filing_accession, document_name)

    if text is None:
        raise ValueError(
            f"Document '{document_name}' for filing {request.filing_accession} is not available "
            f"in offline data. The filings/ directory may not contain this file. "
            f"To access this document, use online mode (EDGAR_OFFLINE_MODE=false) which "
            f"fetches directly from SEC EDGAR."
        )

    # Build URL for reference
    cik_stripped = cik.lstrip("0")
    accession_clean = request.filing_accession.replace("-", "")
    base_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{accession_clean}"

    file_path = None

    # Write to filesystem if APP_FS_ROOT is set (RL Studio environment)
    fs_root = os.getenv("APP_FS_ROOT")
    if fs_root:
        doc_dir = Path(fs_root) / "edgar_documents"
        doc_dir.mkdir(parents=True, exist_ok=True)

        safe_filename = document_name.replace("/", "_")
        file_name = f"{request.filing_accession}_{safe_filename}"
        file_path = doc_dir / file_name

        file_path.write_text(text, encoding="utf-8")
        file_path = str(file_path)

        # Return preview instead of full text
        preview_len = 10_000
        if len(text) > preview_len:
            text = text[:preview_len] + f"\n\n[TRUNCATED - Full content at: {file_path}]"

    return GetFilingDocumentOutput(
        filing_accession=request.filing_accession,
        filename=document_name,
        text=text,
        url=f"{base_url}/{document_name}",
        file_path=file_path,
        data_source="offline_filings",
    )


# =============================================================================
# Public tool functions
# =============================================================================


async def list_filing_documents(request: ListFilingDocumentsInput) -> ListFilingDocumentsOutput:
    """List all documents in a SEC filing."""
    if EDGAR_OFFLINE_MODE:
        return await _list_filing_documents_offline(request)

    await resolve_cik(request.cik, request.ticker, request.name)

    logger.info(f"Listing documents for filing {request.filing_accession}")

    filing = get_filing_from_accession(request.filing_accession)

    # Build base URL for documents
    # Strip leading zeros from CIK for SEC archive URL format
    cik = str(filing.cik).lstrip("0") if filing.cik else ""
    accession_clean = request.filing_accession.replace("-", "")
    base_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_clean}"

    documents = []

    # Get primary document(s)
    primary_doc = None
    if hasattr(filing, "primary_documents") and filing.primary_documents:
        for doc in filing.primary_documents:
            filename = _get_document_filename(doc)
            primary_doc = primary_doc or filename
            documents.append(
                FilingDocumentInfo(
                    filename=filename,
                    description="Primary document",
                    url=f"{base_url}/{filename}",
                )
            )

    # Get attachments/exhibits
    if hasattr(filing, "attachments") and filing.attachments:
        for att in filing.attachments:
            filename = _get_document_filename(att)
            description = getattr(att, "description", None)
            documents.append(
                FilingDocumentInfo(
                    filename=filename,
                    description=description,
                    url=f"{base_url}/{filename}",
                )
            )

    return ListFilingDocumentsOutput(
        filing_accession=request.filing_accession,
        primary_document=primary_doc,
        documents=documents,
        total_documents=len(documents),
        data_source="edgartools",
    )


async def get_filing_document(request: GetFilingDocumentInput) -> GetFilingDocumentOutput:
    """Get text content of a SEC filing document."""
    if EDGAR_OFFLINE_MODE:
        return await _get_filing_document_offline(request)

    await resolve_cik(request.cik, request.ticker, request.name)

    logger.info(f"Fetching document '{request.document}' from filing {request.filing_accession}")

    filing = get_filing_from_accession(request.filing_accession)

    # Build URL
    # Strip leading zeros from CIK for SEC archive URL format
    cik = str(filing.cik).lstrip("0") if filing.cik else ""
    accession_clean = request.filing_accession.replace("-", "")
    base_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_clean}"

    if request.document == "primary":
        # Get primary document text - try filing-level methods first
        text = None
        text_source = "none"

        # Debug: log available methods on filing
        logger.debug(f"DEBUG: filing type = {type(filing)}")
        filing_text_callable = (
            callable(getattr(filing, "text", None)) if hasattr(filing, "text") else "N/A"
        )
        logger.debug(
            f"DEBUG: filing.text exists = {hasattr(filing, 'text')}, "
            f"callable = {filing_text_callable}"
        )
        filing_html_callable = (
            callable(getattr(filing, "html", None)) if hasattr(filing, "html") else "N/A"
        )
        logger.debug(
            f"DEBUG: filing.html exists = {hasattr(filing, 'html')}, "
            f"callable = {filing_html_callable}"
        )

        # Try filing.text() first
        if hasattr(filing, "text") and callable(filing.text):
            candidate = filing.text()
            candidate_len = len(candidate) if candidate else 0
            candidate_preview = repr(candidate[:200]) if candidate else "None/Empty"
            logger.debug(
                f"DEBUG: filing.text() returned {candidate_len} chars, preview: {candidate_preview}"
            )
            if not _is_edgartools_repr_error(candidate):
                text = candidate
                text_source = "filing.text()"

        # Try filing.html() if text() failed or returned error
        if not text and hasattr(filing, "html") and callable(filing.html):
            candidate = filing.html()
            candidate_len = len(candidate) if candidate else 0
            candidate_preview = repr(candidate[:200]) if candidate else "None/Empty"
            logger.debug(
                f"DEBUG: filing.html() returned {candidate_len} chars, preview: {candidate_preview}"
            )
            if not _is_edgartools_repr_error(candidate):
                text = candidate
                text_source = "filing.html()"

        # Get primary document filename and try document-level methods if filing-level failed
        filename = None
        if hasattr(filing, "primary_documents") and filing.primary_documents:
            primary_doc = filing.primary_documents[0]
            filename = _get_document_filename(primary_doc)

            logger.debug(f"DEBUG: primary_doc type = {type(primary_doc)}")
            primary_doc_text_callable = (
                callable(getattr(primary_doc, "text", None))
                if hasattr(primary_doc, "text")
                else "N/A"
            )
            logger.debug(
                f"DEBUG: primary_doc.text exists = {hasattr(primary_doc, 'text')}, "
                f"callable = {primary_doc_text_callable}"
            )
            primary_doc_html_callable = (
                callable(getattr(primary_doc, "html", None))
                if hasattr(primary_doc, "html")
                else "N/A"
            )
            logger.debug(
                f"DEBUG: primary_doc.html exists = {hasattr(primary_doc, 'html')}, "
                f"callable = {primary_doc_html_callable}"
            )
            logger.debug(f"DEBUG: primary_doc.content exists = {hasattr(primary_doc, 'content')}")

            # If filing-level text extraction failed or returned error, try document-level methods
            if not text:
                if hasattr(primary_doc, "text") and callable(primary_doc.text):
                    candidate = primary_doc.text()
                    candidate_len = len(candidate) if candidate else 0
                    candidate_preview = repr(candidate[:200]) if candidate else "None/Empty"
                    logger.debug(
                        f"DEBUG: primary_doc.text() returned {candidate_len} chars, "
                        f"preview: {candidate_preview}"
                    )
                    if not _is_edgartools_repr_error(candidate):
                        text = candidate
                        text_source = "primary_doc.text()"

                if not text and hasattr(primary_doc, "html") and callable(primary_doc.html):
                    candidate = primary_doc.html()
                    candidate_len = len(candidate) if candidate else 0
                    candidate_preview = repr(candidate[:200]) if candidate else "None/Empty"
                    logger.debug(
                        f"DEBUG: primary_doc.html() returned {candidate_len} chars, "
                        f"preview: {candidate_preview}"
                    )
                    if not _is_edgartools_repr_error(candidate):
                        text = candidate
                        text_source = "primary_doc.html()"

                if not text and hasattr(primary_doc, "content"):
                    candidate = primary_doc.content
                    candidate_len = len(candidate) if candidate else 0
                    candidate_preview = repr(candidate[:200]) if candidate else "None/Empty"
                    logger.debug(
                        f"DEBUG: primary_doc.content returned {candidate_len} chars, "
                        f"preview: {candidate_preview}"
                    )
                    if not _is_edgartools_repr_error(candidate):
                        text = candidate
                        text_source = "primary_doc.content"
        elif hasattr(filing, "document"):
            filename = filing.document

        # Final fallback
        if not text:
            text = ""
            logger.debug("DEBUG: No valid text found, using empty string fallback")

        logger.info(f"DEBUG: Final text source = {text_source}, length = {len(text)}")

    else:
        # Find specific document by filename
        text = None
        filename = request.document
        document_found = False

        # Check primary documents first
        if hasattr(filing, "primary_documents") and filing.primary_documents:
            for doc in filing.primary_documents:
                doc_filename = _get_document_filename(doc)
                if doc_filename == request.document:
                    document_found = True
                    # Try each method, checking for edgartools __repr__ error
                    if hasattr(doc, "text") and callable(doc.text):
                        candidate = doc.text()
                        if not _is_edgartools_repr_error(candidate):
                            text = candidate
                    if not text and hasattr(doc, "html") and callable(doc.html):
                        candidate = doc.html()
                        if not _is_edgartools_repr_error(candidate):
                            text = candidate
                    if not text and hasattr(doc, "content"):
                        candidate = doc.content
                        if not _is_edgartools_repr_error(candidate):
                            text = candidate
                    break

        # Check attachments if not found in primary
        if not document_found and hasattr(filing, "attachments") and filing.attachments:
            for att in filing.attachments:
                att_filename = _get_document_filename(att)
                if att_filename == request.document:
                    document_found = True
                    # Try each method, checking for edgartools __repr__ error
                    if hasattr(att, "text") and callable(att.text):
                        candidate = att.text()
                        if not _is_edgartools_repr_error(candidate):
                            text = candidate
                    if not text and hasattr(att, "html") and callable(att.html):
                        candidate = att.html()
                        if not _is_edgartools_repr_error(candidate):
                            text = candidate
                    if not text and hasattr(att, "content"):
                        candidate = att.content
                        if not _is_edgartools_repr_error(candidate):
                            text = candidate
                    break

        if not document_found:
            raise ValueError(
                f"Document '{request.document}' not found in filing {request.filing_accession}"
            )

    text = text or ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    file_path = None

    # Write to filesystem if APP_FS_ROOT is set (RL Studio environment)
    fs_root = os.getenv("APP_FS_ROOT")
    if fs_root:
        doc_dir = Path(fs_root) / "edgar_documents"
        doc_dir.mkdir(parents=True, exist_ok=True)

        safe_filename = (filename or "primary").replace("/", "_")
        file_name = f"{request.filing_accession}_{safe_filename}"
        file_path = doc_dir / file_name

        file_path.write_text(text, encoding="utf-8")
        file_path = str(file_path)

        # Return preview instead of full text
        preview_len = 10_000
        if len(text) > preview_len:
            text = text[:preview_len] + f"\n\n[TRUNCATED - Full content at: {file_path}]"

    return GetFilingDocumentOutput(
        filing_accession=request.filing_accession,
        filename=filename or "primary",
        text=text,
        url=f"{base_url}/{filename}" if filename else base_url,
        file_path=file_path,
        data_source="edgartools",
    )
