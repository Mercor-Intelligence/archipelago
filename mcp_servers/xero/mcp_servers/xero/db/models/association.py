"""Association model for file-to-object links (Files API)."""

from sqlalchemy import Column, String

from mcp_servers.xero.db.session import Base


class Association(Base):
    """Association database model for file-to-object links (Files API)."""

    __tablename__ = "associations"

    # Composite primary key: file_id + object_id
    id = Column(String, primary_key=True)  # Generated as file_id + "_" + object_id
    file_id = Column(String, nullable=False)
    object_id = Column(String, nullable=False)
    object_type = Column(String, nullable=True)  # Invoice, Contact, etc.
    object_group = Column(String, nullable=True)  # Account, Contact, etc.

    def to_dict(self) -> dict:
        """Convert to Xero Files API format (PascalCase)."""
        return {
            "FileId": self.file_id,
            "ObjectId": self.object_id,
            "ObjectType": self.object_type,
            "ObjectGroup": self.object_group,
        }

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict (supports both API format and CSV format)."""
        file_id = data.get("FileId") or data.get("file_id")
        object_id = data.get("ObjectId") or data.get("object_id")

        # Validate required fields for composite ID generation
        if not file_id or not object_id:
            raise ValueError(
                f"Both file_id and object_id are required for Association. "
                f"Got file_id={file_id!r}, object_id={object_id!r}"
            )

        # Generate composite ID
        association_id = data.get("id") or f"{file_id}_{object_id}"

        return cls(
            id=association_id,
            file_id=file_id,
            object_id=object_id,
            object_type=data.get("ObjectType") or data.get("object_type"),
            object_group=data.get("ObjectGroup") or data.get("object_group"),
        )
