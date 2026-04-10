"""File model for file metadata (Files API)."""

import json

from sqlalchemy import Column, Integer, String, Text

from mcp_servers.xero.db.session import Base


class File(Base):
    """File database model for file metadata (Files API)."""

    __tablename__ = "files"

    file_id = Column(String, primary_key=True)
    name = Column(String, nullable=True)
    mime_type = Column(String, nullable=True)
    size = Column(Integer, nullable=True)
    created_date_utc = Column(String, nullable=True)
    updated_date_utc = Column(String, nullable=True)
    user_info = Column(Text, nullable=True)  # JSON
    folder_id = Column(String, nullable=True)

    def to_dict(self) -> dict:
        """Convert to Xero Files API format (PascalCase)."""
        result: dict = {
            "Id": self.file_id,
            "Name": self.name,
            "MimeType": self.mime_type,
            "Size": self.size,
            "CreatedDateUtc": self.created_date_utc,
            "UpdatedDateUtc": self.updated_date_utc,
            "FolderId": self.folder_id,
        }
        if self.user_info is not None:
            result["User"] = json.loads(str(self.user_info))
        return result

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict (supports both API format and CSV format)."""
        user_info = data.get("User") or data.get("user_info")

        # Handle JSON strings from CSV (already serialized)
        if isinstance(user_info, str):
            user_info_json = user_info
        else:
            user_info_json = json.dumps(user_info) if user_info else None

        # Handle size field - use 'in' check to preserve zero values
        # Treat empty strings as None for numeric conversion
        size = data["Size"] if "Size" in data else data.get("size")
        if size is not None and size != "":
            size = int(size)
        elif size == "":
            size = None

        return cls(
            file_id=data.get("Id") or data.get("file_id"),
            name=data.get("Name") or data.get("name"),
            mime_type=data.get("MimeType") or data.get("mime_type"),
            size=size,
            created_date_utc=data.get("CreatedDateUtc") or data.get("created_date_utc"),
            updated_date_utc=data.get("UpdatedDateUtc") or data.get("updated_date_utc"),
            user_info=user_info_json,
            folder_id=data.get("FolderId") or data.get("folder_id"),
        )
