"""Generic API response model for catch-all storage of other endpoints."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class GenericApiResponse(Base):
    """Catch-all storage for API responses not covered by specialized tables.

    Stores any FMP API endpoint response that doesn't have a dedicated table,
    allowing flexible offline support for all 173 tools.

    The endpoint and params_hash together form a unique key, allowing
    efficient lookup of cached responses.

    Attributes:
        id: Auto-increment primary key
        endpoint: API endpoint path (e.g., '/stock/list', '/market-cap/AAPL')
        params_hash: SHA256 hash of sorted query parameters
        params_json: Original query parameters as JSON string
        response_data: Full JSON response from API
        symbol: Optional symbol if endpoint is symbol-specific
        http_status: HTTP status code from original response
        created_at: Record creation timestamp
        updated_at: Record last update timestamp
    """

    __tablename__ = "generic_api_responses"

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Endpoint identification (unique together, indexed via __table_args__)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA256 hash of params

    # Original parameters (for debugging/transparency)
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional symbol for symbol-specific endpoints (indexed via __table_args__)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Response data
    response_data: Mapped[str] = mapped_column(Text, nullable=False)

    # HTTP metadata
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True, default=200)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Indexes and constraints
    __table_args__ = (
        UniqueConstraint(
            "endpoint",
            "params_hash",
            name="uq_generic_api_responses_endpoint_params_hash",
        ),
        Index("ix_generic_api_responses_endpoint", "endpoint"),
        Index("ix_generic_api_responses_symbol", "symbol"),
        Index("ix_generic_api_responses_endpoint_symbol", "endpoint", "symbol"),
    )

    def __repr__(self) -> str:
        hash_preview = self.params_hash[:8] if self.params_hash else None
        return f"<GenericApiResponse(endpoint={self.endpoint!r}, params_hash={hash_preview}...)>"
