"""Folder model for folder organization (Files API)."""

from sqlalchemy import Boolean, Column, Integer, String

from mcp_servers.xero.db.session import Base


def parse_bool(value) -> bool:
    """Parse boolean from various formats (bool, string, None)."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


class Folder(Base):
    """Folder database model for folder organization (Files API)."""

    __tablename__ = "folders"

    folder_id = Column(String, primary_key=True)
    name = Column(String, nullable=True)
    file_count = Column(Integer, nullable=True)
    email = Column(String, nullable=True)  # Only for Inbox folder
    is_inbox = Column(Boolean, default=False)

    def to_dict(self) -> dict:
        """Convert to Xero Files API format (PascalCase)."""
        result: dict = {
            "Id": self.folder_id,
            "Name": self.name,
            "FileCount": self.file_count,
            "IsInbox": self.is_inbox,
        }
        if self.email is not None:
            result["Email"] = self.email
        return result

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict (supports both API format and CSV format)."""
        # Handle file count field - use 'in' check to preserve zero values
        # Treat empty strings as None for numeric conversion
        file_count = data["FileCount"] if "FileCount" in data else data.get("file_count")
        if file_count is not None and file_count != "":
            file_count = int(file_count)
        elif file_count == "":
            file_count = None

        return cls(
            folder_id=data.get("Id") or data.get("folder_id"),
            name=data.get("Name") or data.get("name"),
            file_count=file_count,
            email=data.get("Email") or data.get("email"),
            is_inbox=parse_bool(data["IsInbox"] if "IsInbox" in data else data.get("is_inbox")),
        )
