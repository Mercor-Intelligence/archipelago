"""Offline repository implementation - reads from local files."""

import json
from pathlib import Path

from config import REPO_ROOT
from loguru import logger


class OfflineEdgarRepository:
    """Repository that reads EDGAR data from local files.

    Expected directory structure:
        {data_dir}/
        ├── company_tickers.json
        ├── sp500_tickers.json (optional)
        ├── submissions/
        │   ├── CIK0000320193.json
        │   └── ...
        ├── companyfacts/
        │   ├── CIK0000320193.json
        │   └── ...
        └── filings/ (optional - for raw 10-X HTML files)
            ├── 0000320193-24-000123/
            │   └── aapl-20240928.htm
            └── ...
    """

    def __init__(self, data_dir: str | Path):
        """Initialize offline repository.

        Args:
            data_dir: Path to the offline data directory
        """
        self.data_dir = Path(data_dir)
        self._company_tickers_cache: dict | None = None

        if not self.data_dir.exists() or self._is_empty_data_dir():
            # Check if offline data exists anywhere accessible
            found_locations = self._find_offline_data_locations()

            if found_locations:
                raise ValueError(
                    f"Offline data directory not found at expected location: {self.data_dir}\n"
                    f"However, offline data was found at: {found_locations}\n"
                    f"Please move/copy the data to: {self.data_dir}"
                )
            else:
                raise ValueError(
                    f"Offline data not found.\n"
                    f"Expected location: {self.data_dir}\n\n"
                    f"No 'edgar_offline' or 'offline_data' folder with data was found "
                    f"anywhere in the repository.\n"
                    f"Please run: python -m zipfile -e data/edgar_offline.zip ./offline_data"
                )

        logger.info(f"Initialized OfflineEdgarRepository with data_dir={self.data_dir}")

    def _is_empty_data_dir(self) -> bool:
        """Check if the data directory exists but is empty or lacks required files."""
        if not self.data_dir.exists():
            return True

        # Check for required files/directories
        required_paths = [
            self.data_dir / "company_tickers.json",
            self.data_dir / "submissions",
            self.data_dir / "companyfacts",
        ]

        # If none of the required paths exist, the directory is effectively empty
        return not any(p.exists() for p in required_paths)

    def _find_offline_data_locations(self) -> list[Path]:
        """Search for offline data folders that contain actual data.

        Looks for both 'edgar_offline' and 'offline_data' directories
        in common locations within the repo and state directories.
        """
        found = []
        repo_root = REPO_ROOT

        # Candidate directory names for offline data
        candidate_names = ["edgar_offline", "offline_data"]

        # Common search locations
        search_roots = [
            repo_root,
            repo_root / "data",
            Path.cwd(),
            Path.cwd() / "data",
        ]

        # Add data_dir's parent if different from above
        if self.data_dir.parent not in search_roots:
            search_roots.append(self.data_dir.parent)

        for root in search_roots:
            if not root.exists():
                continue

            # Check direct children first (most common case)
            for name in candidate_names:
                candidate = root / name
                if candidate.is_dir() and self._has_offline_data(candidate):
                    if candidate not in found:
                        found.append(candidate)

            # Also search recursively for edgar_offline (legacy)
            for candidate in root.rglob("edgar_offline"):
                if candidate.is_dir() and self._has_offline_data(candidate):
                    if candidate not in found:
                        found.append(candidate)

        return found

    def _has_offline_data(self, path: Path) -> bool:
        """Check if a directory contains actual offline data (not just .DS_Store)."""
        return (
            (path / "company_tickers.json").exists()
            or (path / "submissions").exists()
            or (path / "companyfacts").exists()
        )

    async def get_company_tickers(self) -> dict:
        """Get all company ticker/CIK mappings from local file."""
        if self._company_tickers_cache is not None:
            return self._company_tickers_cache

        tickers_file = self.data_dir / "company_tickers.json"
        if not tickers_file.exists():
            raise FileNotFoundError(
                f"Company tickers file not found: {tickers_file}. "
                f"This company may not be in the offline dataset."
            )

        with open(tickers_file, encoding="utf-8") as f:
            self._company_tickers_cache = json.load(f)

        logger.debug(f"Loaded {len(self._company_tickers_cache)} companies from {tickers_file}")
        return self._company_tickers_cache

    async def get_submissions(self, cik: str) -> dict:
        """Get filing submissions for a company from local file.

        Args:
            cik: 10-digit zero-padded CIK (e.g., "0000320193")

        Returns:
            Dict with company info and filing history

        Raises:
            FileNotFoundError: If submissions file doesn't exist for this CIK
        """
        # Ensure CIK is properly formatted
        cik_padded = cik.zfill(10)
        submissions_file = self.data_dir / "submissions" / f"CIK{cik_padded}.json"

        if not submissions_file.exists():
            raise FileNotFoundError(
                f"Submissions not found for CIK {cik_padded}. "
                f"This company may not be in the offline dataset."
            )

        with open(submissions_file, encoding="utf-8") as f:
            data = json.load(f)

        logger.debug(f"Loaded submissions for CIK {cik_padded}")
        return data

    async def get_company_facts(self, cik: str) -> dict:
        """Get all XBRL facts for a company from local file.

        Args:
            cik: 10-digit zero-padded CIK (e.g., "0000320193")

        Returns:
            Dict with all XBRL financial data

        Raises:
            FileNotFoundError: If company facts file doesn't exist for this CIK
        """
        cik_padded = cik.zfill(10)
        facts_file = self.data_dir / "companyfacts" / f"CIK{cik_padded}.json"

        if not facts_file.exists():
            raise FileNotFoundError(
                f"Company facts not found for CIK {cik_padded}. "
                f"This company may not be in the offline dataset."
            )

        with open(facts_file, encoding="utf-8") as f:
            data = json.load(f)

        logger.debug(f"Loaded company facts for CIK {cik_padded}")
        return data

    async def get_company_concept(self, cik: str, taxonomy: str, tag: str) -> dict:
        """Get specific XBRL concept data for a company.

        Extracts the concept from the full company facts file.

        Args:
            cik: 10-digit zero-padded CIK
            taxonomy: XBRL taxonomy (e.g., "us-gaap")
            tag: XBRL tag (e.g., "Revenues")

        Returns:
            Dict with concept data across all filings

        Raises:
            FileNotFoundError: If company facts file doesn't exist
            KeyError: If the concept doesn't exist for this company
        """
        facts = await self.get_company_facts(cik)

        # Extract the specific concept
        taxonomy_facts = facts.get("facts", {}).get(taxonomy, {})
        if tag not in taxonomy_facts:
            raise KeyError(
                f"Concept {taxonomy}:{tag} not found for CIK {cik}. "
                f"Available concepts: {list(taxonomy_facts.keys())[:10]}..."
            )

        # Build response matching SEC API format
        concept_data = taxonomy_facts[tag]
        return {
            "cik": int(cik),
            "taxonomy": taxonomy,
            "tag": tag,
            "label": concept_data.get("label", tag),
            "description": concept_data.get("description", ""),
            "entityName": facts.get("entityName", ""),
            "units": concept_data.get("units", {}),
        }

    async def get_frames(self, taxonomy: str, tag: str, unit: str, period: str) -> dict:
        """Get aggregated XBRL data across all companies.

        NOTE: This requires building an index from all company facts files.
        For offline mode, this scans all companyfacts/*.json files.

        This is slower than the online API but provides the same functionality.

        Args:
            taxonomy: XBRL taxonomy (e.g., "us-gaap")
            tag: XBRL tag (e.g., "Revenues")
            unit: Unit type (e.g., "USD")
            period: Period (e.g., "CY2023Q1I")

        Returns:
            Dict with aggregated data across companies
        """
        facts_dir = self.data_dir / "companyfacts"
        if not facts_dir.exists():
            raise FileNotFoundError(f"Company facts directory not found: {facts_dir}")

        results = []
        for facts_file in facts_dir.glob("CIK*.json"):
            with open(facts_file, encoding="utf-8") as f:
                company_facts = json.load(f)

            # Get the concept data
            concept = company_facts.get("facts", {}).get(taxonomy, {}).get(tag, {})
            units_data = concept.get("units", {}).get(unit, [])

            # Find matching period
            for fact in units_data:
                # Match period format (e.g., "CY2023Q1I" matches frame period)
                fact_frame = fact.get("frame", "")
                if fact_frame == period:
                    results.append(
                        {
                            "accn": fact.get("accn"),
                            "cik": company_facts.get("cik"),
                            "entityName": company_facts.get("entityName"),
                            "loc": fact.get("loc"),
                            "end": fact.get("end"),
                            "val": fact.get("val"),
                        }
                    )
                    break  # One result per company

        logger.info(
            f"Frames query found {len(results)} companies for {taxonomy}:{tag}/{unit}/{period}"
        )

        return {
            "taxonomy": taxonomy,
            "tag": tag,
            "ccp": period,
            "uom": unit,
            "label": tag,
            "description": "",
            "pts": len(results),
            "data": results,
        }

    async def list_filing_documents(self, cik: str, accession: str) -> dict:
        """List documents for a filing from local submissions data.

        Extracts primaryDocument and primaryDocDescription from the submissions
        JSON, and optionally scans the filings/ directory for additional files.

        Args:
            cik: 10-digit zero-padded CIK
            accession: Accession number (e.g., "0000320193-24-000123")

        Returns:
            Dict with primary_document, primary_doc_description, additional_files

        Raises:
            ValueError: If filing accession not found in submissions
        """
        submissions = await self.get_submissions(cik)
        recent = submissions.get("filings", {}).get("recent", {})

        accession_numbers = recent.get("accessionNumber", [])
        primary_documents = recent.get("primaryDocument", [])
        primary_doc_descriptions = recent.get("primaryDocDescription", [])

        # Find the filing index by accession number
        filing_idx = None
        for i, acc in enumerate(accession_numbers):
            if acc == accession:
                filing_idx = i
                break

        if filing_idx is None:
            raise ValueError(
                f"Filing accession '{accession}' not found in submissions for CIK {cik}. "
                f"Available accessions (first 5): {accession_numbers[:5]}"
            )

        primary_doc = primary_documents[filing_idx] if filing_idx < len(primary_documents) else None
        primary_desc = (
            primary_doc_descriptions[filing_idx]
            if filing_idx < len(primary_doc_descriptions)
            else None
        )

        # Scan filings/ directory for additional files if it exists
        additional_files = []
        accession_clean = accession.replace("-", "")
        possible_dirs = [
            self.data_dir / "filings" / accession,
            self.data_dir / "filings" / accession_clean,
            self.data_dir / "filings" / cik.lstrip("0") / accession_clean,
        ]

        for filing_dir in possible_dirs:
            if filing_dir.is_dir():
                for f in sorted(filing_dir.iterdir()):
                    if f.is_file() and f.name != primary_doc:
                        additional_files.append(f.name)
                break  # Use first matching directory

        logger.debug(
            f"Listed documents for {cik}/{accession}: "
            f"primary={primary_doc}, additional={len(additional_files)}"
        )

        return {
            "primary_document": primary_doc,
            "primary_doc_description": primary_desc,
            "additional_files": additional_files,
        }

    async def get_filing_html(self, cik: str, accession: str, document: str) -> str | None:
        """Get HTML content of a filing document from local files.

        Args:
            cik: 10-digit zero-padded CIK
            accession: Accession number (e.g., "0000320193-24-000123")
            document: Primary document filename (e.g., "aapl-20240928.htm")

        Returns:
            HTML content as string, or None if not available
        """
        # Try multiple possible directory structures
        accession_clean = accession.replace("-", "")

        possible_paths = [
            # Structure 1: filings/{accession}/{document}
            self.data_dir / "filings" / accession / document,
            # Structure 2: filings/{accession_no_dashes}/{document}
            self.data_dir / "filings" / accession_clean / document,
            # Structure 3: filings/{cik}/{accession}/{document}
            self.data_dir / "filings" / cik.lstrip("0") / accession_clean / document,
            # Structure 4: 10x_raw/{accession}.htm (SRAF-style)
            self.data_dir / "10x_raw" / f"{accession}.htm",
            # Structure 5: 10x_cleaned/{accession}.txt (SRAF cleaned)
            self.data_dir / "10x_cleaned" / f"{accession}.txt",
        ]

        for path in possible_paths:
            if path.exists():
                logger.debug(f"Found filing at {path}")
                with open(path, encoding="utf-8", errors="ignore") as f:
                    return f.read()

        logger.warning(
            f"Filing HTML not found for {cik}/{accession}/{document}. "
            f"Checked paths: {[str(p) for p in possible_paths]}"
        )
        return None
