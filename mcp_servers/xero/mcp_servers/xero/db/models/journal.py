"""Journal model for manual journal entries."""

import json

from sqlalchemy import Column, Integer, String, Text

from mcp_servers.xero.db.models.invoice import normalize_xero_date
from mcp_servers.xero.db.session import Base


class Journal(Base):
    """Journal database model for manual journal entries."""

    __tablename__ = "journals"

    journal_id = Column(String, primary_key=True)
    journal_number = Column(Integer, nullable=True)
    journal_date = Column(String, nullable=True)
    created_date_utc = Column(String, nullable=True)
    reference = Column(String, nullable=True)
    source_id = Column(String, nullable=True)
    source_type = Column(String, nullable=True)
    journal_lines = Column(Text, nullable=True)  # JSON array

    def to_dict(self) -> dict:
        """Convert to Xero API format."""
        journal_lines_data = (
            json.loads(str(self.journal_lines)) if self.journal_lines is not None else []
        )

        return {
            "JournalID": self.journal_id,
            "JournalNumber": self.journal_number,
            "JournalDate": self.journal_date,
            "CreatedDateUTC": self.created_date_utc,
            "Reference": self.reference,
            "SourceID": self.source_id,
            "SourceType": self.source_type,
            "JournalLines": journal_lines_data,
        }

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict (supports both API format and CSV format)."""
        journal_lines = data.get("JournalLines") or data.get("journal_lines")

        # Handle JSON strings from CSV (already serialized)
        if isinstance(journal_lines, str):
            journal_lines_json = journal_lines
        else:
            journal_lines_json = json.dumps(journal_lines) if journal_lines else "[]"

        # Handle date fields
        raw_journal_date = (
            data.get("JournalDateString") or data.get("JournalDate") or data.get("journal_date")
        )

        # Handle journal_number - use 'in' check to preserve zero values
        # Treat empty strings as None for numeric conversion
        journal_number = (
            data["JournalNumber"] if "JournalNumber" in data else data.get("journal_number")
        )
        if journal_number is not None and journal_number != "":
            journal_number = int(journal_number)
        elif journal_number == "":
            journal_number = None

        return cls(
            journal_id=data.get("JournalID") or data.get("journal_id"),
            journal_number=journal_number,
            journal_date=normalize_xero_date(raw_journal_date),
            created_date_utc=data.get("CreatedDateUTC") or data.get("created_date_utc"),
            reference=data.get("Reference") or data.get("reference"),
            source_id=data.get("SourceID") or data.get("source_id"),
            source_type=data.get("SourceType") or data.get("source_type"),
            journal_lines=journal_lines_json,
        )
