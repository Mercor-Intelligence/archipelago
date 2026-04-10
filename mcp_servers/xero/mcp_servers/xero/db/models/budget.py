"""Budget model for budget entities with tracking categories."""

import json
from typing import Any

from sqlalchemy import Column, String, Text

from mcp_servers.xero.db.session import Base


class Budget(Base):
    """Budget database model for budget entities."""

    __tablename__ = "budgets"

    budget_id = Column(String, primary_key=True)
    type = Column(String, nullable=True)
    description = Column(String, nullable=True)
    updated_date_utc = Column(String, nullable=True)
    tracking = Column(Text, nullable=True)  # JSON array of tracking categories
    budget_lines = Column(Text, nullable=True)  # JSON array of budget lines

    def to_dict(self) -> dict[str, Any]:
        """Convert to Xero API format."""
        tracking_data: list[dict[str, Any]] = json.loads(self.tracking) if self.tracking else []  # type: ignore
        budget_lines_data: list[dict[str, Any]] = (
            json.loads(self.budget_lines) if self.budget_lines else []  # type: ignore
        )

        # Transform budget lines to include BudgetBalances structure
        # The synthetic data stores them flat, but the API expects BudgetBalances array
        transformed_lines: list[dict[str, Any]] = []
        if budget_lines_data:
            # Group by AccountID
            lines_by_account: dict[str, dict[str, Any]] = {}
            for line in budget_lines_data:
                account_id: str = line.get("AccountID", "")
                if account_id not in lines_by_account:
                    lines_by_account[account_id] = {
                        "AccountID": account_id,
                        "AccountCode": line.get("AccountCode"),
                        "BudgetBalances": [],
                    }
                budget_balances: list[dict[str, Any]] = lines_by_account[account_id][
                    "BudgetBalances"
                ]
                budget_balances.append({"Period": line.get("Period"), "Amount": line.get("Amount")})
            transformed_lines = list(lines_by_account.values())

        # Ensure each tracking item has an Options array
        for tracking_item in tracking_data:
            if "Options" not in tracking_item:
                tracking_item["Options"] = []

        return {
            "BudgetID": self.budget_id,
            "Type": self.type,
            "Description": self.description,
            "UpdatedDateUTC": self.updated_date_utc,
            "Tracking": tracking_data,
            "BudgetLines": transformed_lines,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Budget":
        """Create from dict (supports both API format and CSV format)."""
        tracking = data.get("Tracking") or data.get("tracking")
        budget_lines = data.get("BudgetLines") or data.get("budget_lines")

        # Handle JSON strings from CSV (already serialized)
        if isinstance(tracking, str):
            tracking_json = tracking
        else:
            tracking_json = json.dumps(tracking) if tracking else "[]"

        if isinstance(budget_lines, str):
            budget_lines_json = budget_lines
        else:
            budget_lines_json = json.dumps(budget_lines) if budget_lines else "[]"

        return cls(
            budget_id=data.get("BudgetID") or data.get("budget_id"),
            type=data.get("Type") or data.get("type"),
            description=data.get("Description") or data.get("description"),
            updated_date_utc=data.get("UpdatedDateUTC") or data.get("updated_date_utc"),
            tracking=tracking_json,
            budget_lines=budget_lines_json,
        )
