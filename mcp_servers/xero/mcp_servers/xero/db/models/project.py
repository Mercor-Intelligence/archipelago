"""Project model for project tracking (Projects API)."""

import json

from sqlalchemy import Column, Integer, String, Text

from mcp_servers.xero.db.session import Base


class Project(Base):
    """Project database model for project tracking (Projects API)."""

    __tablename__ = "projects"

    project_id = Column(String, primary_key=True)
    contact_id = Column(String, nullable=True)
    name = Column(String, nullable=True)
    currency_code = Column(String, nullable=True)
    minutes_logged = Column(Integer, nullable=True)
    total_task_amount = Column(Text, nullable=True)  # JSON with currency and value
    total_expense_amount = Column(Text, nullable=True)  # JSON with currency and value
    minutes_to_be_invoiced = Column(Integer, nullable=True)
    estimate = Column(Text, nullable=True)  # JSON with currency and value
    status = Column(String, nullable=True)  # INPROGRESS, CLOSED
    deadline_utc = Column(String, nullable=True)
    total_invoiced = Column(Text, nullable=True)  # JSON with currency and value
    total_to_be_invoiced = Column(Text, nullable=True)  # JSON with currency and value
    deposit = Column(Text, nullable=True)  # JSON with currency and value

    def to_dict(self) -> dict:
        """Convert to Xero Projects API format (camelCase)."""
        result: dict = {
            "projectId": self.project_id,
            "contactId": self.contact_id,
            "name": self.name,
            "currencyCode": self.currency_code,
            "minutesLogged": self.minutes_logged,
            "status": self.status,
        }
        if self.total_task_amount is not None:
            result["totalTaskAmount"] = json.loads(str(self.total_task_amount))
        if self.total_expense_amount is not None:
            result["totalExpenseAmount"] = json.loads(str(self.total_expense_amount))
        if self.minutes_to_be_invoiced is not None:
            result["minutesToBeInvoiced"] = self.minutes_to_be_invoiced
        if self.estimate is not None:
            result["estimate"] = json.loads(str(self.estimate))
        if self.deadline_utc is not None:
            result["deadlineUtc"] = self.deadline_utc
        if self.total_invoiced is not None:
            result["totalInvoiced"] = json.loads(str(self.total_invoiced))
        if self.total_to_be_invoiced is not None:
            result["totalToBeInvoiced"] = json.loads(str(self.total_to_be_invoiced))
        if self.deposit is not None:
            result["deposit"] = json.loads(str(self.deposit))
        return result

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict (supports both API format and CSV format)."""
        # Handle JSON fields
        total_task_amount = data.get("totalTaskAmount") or data.get("total_task_amount")
        total_expense_amount = data.get("totalExpenseAmount") or data.get("total_expense_amount")
        estimate = data.get("estimate")
        total_invoiced = data.get("totalInvoiced") or data.get("total_invoiced")
        total_to_be_invoiced = data.get("totalToBeInvoiced") or data.get("total_to_be_invoiced")
        deposit = data.get("deposit")

        # Handle JSON strings from CSV (already serialized)
        def serialize_json(value):
            if isinstance(value, str):
                return value
            return json.dumps(value) if value else None

        # Handle integer fields - use 'in' check to preserve zero values
        # Treat empty strings as None for numeric conversion
        minutes_logged = (
            data["minutesLogged"] if "minutesLogged" in data else data.get("minutes_logged")
        )
        if minutes_logged is not None and minutes_logged != "":
            minutes_logged = int(minutes_logged)
        elif minutes_logged == "":
            minutes_logged = None

        minutes_to_be_invoiced = (
            data["minutesToBeInvoiced"]
            if "minutesToBeInvoiced" in data
            else data.get("minutes_to_be_invoiced")
        )
        if minutes_to_be_invoiced is not None and minutes_to_be_invoiced != "":
            minutes_to_be_invoiced = int(minutes_to_be_invoiced)
        elif minutes_to_be_invoiced == "":
            minutes_to_be_invoiced = None

        return cls(
            project_id=data.get("projectId") or data.get("project_id"),
            contact_id=data.get("contactId") or data.get("contact_id"),
            name=data.get("name"),
            currency_code=data.get("currencyCode") or data.get("currency_code"),
            minutes_logged=minutes_logged,
            total_task_amount=serialize_json(total_task_amount),
            total_expense_amount=serialize_json(total_expense_amount),
            minutes_to_be_invoiced=minutes_to_be_invoiced,
            estimate=serialize_json(estimate),
            status=data.get("status"),
            deadline_utc=data.get("deadlineUtc") or data.get("deadline_utc"),
            total_invoiced=serialize_json(total_invoiced),
            total_to_be_invoiced=serialize_json(total_to_be_invoiced),
            deposit=serialize_json(deposit),
        )
