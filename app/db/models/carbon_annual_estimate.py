"""Durable annual carbon summaries; temporary Earth Engine URLs are excluded."""
from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CarbonAnnualEstimate(Base):
    __tablename__ = "carbon_annual_estimates"
    __table_args__ = (Index("ix_carbon_annual_estimates_year_pool", "analysis_year", "dataset_key"),)

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    analysis_year: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_year: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_key: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    aoi_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_through: Mapped[date] = mapped_column(Date, nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
