"""Source models for Greenhouse MCP Server.

API Reference:
- GET /sources
"""

from db.models.base import Base
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship


class SourceType(Base):
    """Source type (referral strategy category).

    Response: { id, name }
    Valid names: attend_events, referrals, third_party_boards, candidate_search,
                 other, social_media, company_marketing, agencies, prospecting
    """

    __tablename__ = "source_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String,
        nullable=False,
        info={
            "enum": [
                "attend_events",
                "referrals",
                "third_party_boards",
                "candidate_search",
                "other",
                "social_media",
                "company_marketing",
                "agencies",
                "prospecting",
            ]
        },
    )


class Source(Base):
    """Candidate source (where they came from).

    Response: { id, name, type: { id, name } }
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    type_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("source_types.id"), nullable=True
    )

    # Relationships
    source_type: Mapped["SourceType | None"] = relationship("SourceType")
