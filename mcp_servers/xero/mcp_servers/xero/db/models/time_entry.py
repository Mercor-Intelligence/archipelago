"""Time entry model for project time entries (Projects API)."""

from sqlalchemy import Column, Integer, String, Text

from mcp_servers.xero.db.session import Base


class TimeEntry(Base):
    """Time entry database model for project time entries (Projects API)."""

    __tablename__ = "time_entries"

    time_entry_id = Column(String, primary_key=True)
    user_id = Column(String, nullable=True)
    project_id = Column(String, nullable=True)
    task_id = Column(String, nullable=True)
    date_utc = Column(String, nullable=True)
    duration = Column(Integer, nullable=True)  # Duration in minutes
    description = Column(Text, nullable=True)
    status = Column(String, nullable=True)  # ACTIVE, LOCKED, INVOICED

    def to_dict(self) -> dict:
        """Convert to Xero Projects API format (camelCase)."""
        return {
            "timeEntryId": self.time_entry_id,
            "userId": self.user_id,
            "projectId": self.project_id,
            "taskId": self.task_id,
            "dateUtc": self.date_utc,
            "duration": self.duration,
            "description": self.description,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict (supports both API format and CSV format)."""
        # Handle duration field - treat empty strings as None for numeric conversion
        duration = data.get("duration")
        if duration is not None and duration != "":
            duration = int(duration)
        elif duration == "":
            duration = None

        return cls(
            time_entry_id=data.get("timeEntryId") or data.get("time_entry_id"),
            user_id=data.get("userId") or data.get("user_id"),
            project_id=data.get("projectId") or data.get("project_id"),
            task_id=data.get("taskId") or data.get("task_id"),
            date_utc=data.get("dateUtc") or data.get("date_utc"),
            duration=duration,
            description=data.get("description"),
            status=data.get("status"),
        )
